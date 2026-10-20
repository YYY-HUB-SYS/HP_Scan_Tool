"""
扫描历史管理器 — HP Scan Tool v4.0 — 轻量 JSON 日志
每次扫描记录元数据到 history.json，支持追加/列表/清空。
"""

import json
import logging
import os
import shutil
import threading
from datetime import datetime

import config_io
from config_io import (
    atomic_write_json as _atomic_write_json,
    config_dir,
)

logger = logging.getLogger(__name__)

_app_dir = None


def _get_app_dir():
    global _app_dir
    if _app_dir is None:
        _app_dir = config_dir()
    return _app_dir


HISTORY_FILE = None  # 延迟初始化


def _history_file():
    global HISTORY_FILE
    if HISTORY_FILE is None:
        HISTORY_FILE = os.path.join(_get_app_dir(), "history.json")
    return HISTORY_FILE


_lock = threading.Lock()

# ── 原子写 + 跨进程锁 ──────────────────────────────────────
# 实现已收口到共享模块 config_io：
#   atomic_write_json —— 唯一临时文件 + 写后校验 + os.replace 原子替换
#   config_write_lock —— msvcrt 跨进程排他锁（同目录 <name>.lock，常驻不删），
#                        **严格版**：拿不到锁抛 ConfigLockTimeout，事务体不执行

# 已备份过的损坏文件（路径 + mtime + size），避免反复读取刷出一堆备份
_CORRUPT_BACKED_UP = set()

#: 损坏备份名撞名时的最大重试次数（同名 ⇒ 自动加序号 -2 / -3 …）
_BACKUP_NAME_MAX_RETRY = 999


class _HistoryReadError(Exception):
    """
    历史文件**读不出来**（OSError：被杀毒/索引程序独占、PermissionError…）。

    与"文件损坏"必须区分开：
      - 损坏（JSON 解析失败 / 顶层不是数组）⇒ 原文已备份，可以从空开始写
      - 读不出来 ⇒ 我们**根本不知道**文件里有什么 ⇒ 绝不能用空列表覆盖它
    """


def append(entry: dict) -> bool:
    """追加一条扫描记录。线程安全 + 跨进程安全（读-改-写全程持**严格**锁）。

    返回是否真的追加成功：
    - True  = 已落盘
    - False = 本次**没有写入**（拿不到跨进程锁 / 读不出来 / 写失败）

    三种失败各有各的语义，**绝不**合并成"静默丢数据"：
    1. 拿不到跨进程锁（ConfigLockTimeout）⇒ **整个"读-改-写"事务放弃**。
       严格锁在**事务体开始前**就抛异常 ⇒ `_load()` 根本没被调用 ⇒
       读失败**不会**被当成"历史为空" ⇒ 原历史文件原样保留（F-7 的护栏不被改坏）。
    2. 读不出来（OSError ⇒ _HistoryReadError）⇒ 放弃本次写入。
       读不出来 ≠ 内容为空，用空列表覆盖会把用户的全部历史静默清空。
    3. 其它异常（写失败等）⇒ 放弃本次写入并记 error 日志。

    ★ 锁作用域（终审 I-2）：**跨进程锁在外、进程内锁在内**。
      旧结构是「进程内锁 ⇒ 跨进程锁」⇒ 等跨进程锁（最长 30 秒）时
      **攥着进程内锁** ⇒ 同进程其它线程（GUI 线程读一次历史）被连带
      堵住 ⇒ 界面假死最长 30 秒。
      现在等跨进程锁期间**不持有任何进程内锁**；进程内锁只保护
      "读盘 → 改 → 写盘"那一小段事务体（见下方注释）。
    """
    entry.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    fpath = _history_file()
    try:
        # ── 跨进程锁（**最外层**）──────────────────────────────
        # ★ 等待期间**不持有**进程内锁 ⇒ 同进程的读线程不被连带堵住。
        with config_io.config_write_lock(fpath):
            # ── 进程内锁（只保护事务体这一小段）───────────────
            # 事务体整体在进程内锁内 ⇒ 同进程内不可能有两个线程同时做
            # 读-改-写 ⇒ 不丢更新（既有护栏：8 线程 × 25 次 ⇒ 200/200）。
            # 跨进程那一侧由上面的严格锁保证（两个进程各持一把文件锁）。
            with _lock:
                records = _load()
                records.append(entry)
                # 保留最近 500 条
                if len(records) > 500:
                    records = records[-500:]
                if not _save(records):
                    return False
    except config_io.ConfigLockTimeout as e:
        logger.error("扫描历史追加被跳过（拿不到跨进程锁，本次未写入）: %s（%s）", fpath, e)
        return False
    except _HistoryReadError as e:
        logger.error("扫描历史读取失败，本次追加已放弃（不覆盖原文件）: %s", e)
        return False
    except Exception as e:
        logger.error("追加扫描历史失败: %s", e)
        return False
    return True


def list_records(limit: int = 50) -> list:
    """返回最近 limit 条记录，最新的在前。"""
    with _lock:
        try:
            records = _load()
        except _HistoryReadError:
            return []
    return list(reversed(records[-limit:]))


def clear() -> bool:
    """清空所有历史记录。

    拿不到跨进程锁 ⇒ 本次**不清空**（原历史保持不变）并返回 False。

    ★ 锁作用域同 append：**跨进程锁在外、进程内锁在内**（终审 I-2）。
    """
    fpath = _history_file()
    try:
        with config_io.config_write_lock(fpath):
            with _lock:
                if not _save([]):
                    return False
    except config_io.ConfigLockTimeout as e:
        logger.error("清空扫描历史被跳过（拿不到跨进程锁，本次未写入）: %s（%s）", fpath, e)
        return False
    except Exception as e:
        logger.error("清空扫描历史失败: %s", e)
        return False
    return True


def _remove_quietly(path: str) -> None:
    """删文件，失败不抛（备份失败时清理残留的占位空文件）"""
    try:
        os.remove(path)
    except OSError:
        pass


def _reserve_backup_path(base_path: str):
    """
    预留一个**唯一**的损坏备份路径（独占创建），返回该路径；文件已建好、内容待写入。

    ★ 为什么不能只靠时间戳：Windows 系统时钟分辨率只有 **15.625ms**（实测
      `time.get_clock_info('time').resolution == 0.015625`）⇒ 相邻两次
      `datetime.now().strftime('%f')` 有 49/50 概率**完全相同** ⇒ 两次损坏
      备份算出**同一个文件名** ⇒ 第二次**覆盖**第一次 ⇒ **证据丢失**。

    ★ 这里的分工：**时间戳只负责可读性**（人一眼看出大概何时损坏），
      **唯一性由「独占创建（O_CREAT|O_EXCL）+ 同名自动加序号」保证**
      ⇒ 与时钟分辨率**完全无关**。
      首个备份名仍是 `<name>.corrupt-<YYYYMMDD-HHMMSS-ffffff>`（既有断言不变），
      真撞名时追加 `-2` / `-3` …（同前缀、同时间戳，人工仍看得出是同一批备份）。

    返回 None = 所有候选名都被占用 / 创建失败（此时不写任何内容）。
    """
    for idx in range(_BACKUP_NAME_MAX_RETRY + 1):
        candidate = base_path if idx == 0 else "%s-%d" % (base_path, idx + 1)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue          # 名字已被占用 ⇒ 换下一个序号
        except OSError as e:
            logger.error("损坏备份文件创建失败: %s（%s）", candidate, e)
            return None
        os.close(fd)
        return candidate
    logger.error("损坏备份文件名候选全部被占用: %s", base_path)
    return None


def _backup_corrupt(fpath: str):
    """
    把损坏的历史文件原文备份成 <name>.corrupt-<时间戳>，返回备份路径。

    - 时间戳只负责**可读性**；备份名**唯一性**由「独占创建 + 同名加序号」保证
      —— Windows 时钟分辨率只有 15.6ms，两次损坏很容易算出同一个时间戳
      （旧实现会让后一次**覆盖**前一次 ⇒ 证据丢失）
    - 同一份损坏内容只备份一次（按 路径+mtime+size 去重），避免反复读取刷出一堆备份。
    - 备份失败不影响主流程（原文仍在原处，不会被静默清空）。
    """
    try:
        st = os.stat(fpath)
    except OSError:
        return None
    key = (fpath, st.st_mtime_ns, st.st_size)
    if key in _CORRUPT_BACKED_UP:
        return None
    _CORRUPT_BACKED_UP.add(key)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = _reserve_backup_path("%s.corrupt-%s" % (fpath, ts))
    if backup is None:
        return None
    try:
        shutil.copyfile(fpath, backup)
    except OSError as e:
        logger.error("扫描历史损坏备份失败: %s（%s）", fpath, e)
        _remove_quietly(backup)
        return None
    logger.error("扫描历史文件已损坏，原文已备份到: %s", backup)
    return backup


def _load() -> list:
    """
    读取扫描历史。

    区分三种"读不到记录"：
    - 文件不存在 → 正常首次运行，返回 []
    - 文件损坏（解析失败 / 编码乱码 / 顶层不是数组）→ 备份原文 + 记 error 日志，
      返回 []。原文不会丢：损坏内容留在 <name>.corrupt-<时间戳> 备份文件里。
    - **读不出来**（OSError：被杀毒/索引程序独占、PermissionError…）→ 备份原文
      （尽力而为）+ 抛 _HistoryReadError。调用方（append）据此**放弃写入**，
      绝不能用空列表覆盖一个我们根本读不到的历史文件（F-7）。
    """
    fpath = _history_file()
    if not os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("扫描历史文件损坏: %s（%s）", fpath, e)
        _backup_corrupt(fpath)
        return []
    except OSError as e:
        logger.error("扫描历史文件读取失败: %s（%s）", fpath, e)
        # 读不出来也要尽力备份（文件被独占时备份可能失败，但不会抛）
        _backup_corrupt(fpath)
        raise _HistoryReadError(str(e)) from e
    if not isinstance(data, list):
        logger.error("扫描历史文件格式错误（期望数组）: %s", fpath)
        _backup_corrupt(fpath)
        return []
    return data


def _save(records: list) -> bool:
    """
    保存扫描历史（唯一临时文件 + 原子替换 + 写后校验）。

    写失败不抛异常，只记 error 日志；原子替换保证失败时旧文件原样保留。
    返回是否真的写入（False = 写失败，调用方据此放弃本次事务）。
    """
    fpath = _history_file()
    try:
        _atomic_write_json(fpath, records)
    except Exception as e:
        logger.error("扫描历史保存失败: %s", e)
        return False
    return True
