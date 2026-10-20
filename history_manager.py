"""
扫描历史管理器 — HP Scan Tool v4.0 — 轻量 JSON 日志
每次扫描记录元数据到 history.json，支持追加/列表/清空。
"""

import json
import logging
import os
import shutil
import sys
import threading
from datetime import datetime

from config_io import (
    atomic_write_json as _atomic_write_json,
    config_dir,
    config_lock as _file_lock,
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
#   config_lock       —— msvcrt 跨进程排他锁（同目录 <name>.lock，常驻不删）

# 已备份过的损坏文件（路径 + mtime + size），避免反复读取刷出一堆备份
_CORRUPT_BACKED_UP = set()


class _HistoryReadError(Exception):
    """
    历史文件**读不出来**（OSError：被杀毒/索引程序独占、PermissionError…）。

    与"文件损坏"必须区分开：
      - 损坏（JSON 解析失败 / 顶层不是数组）⇒ 原文已备份，可以从空开始写
      - 读不出来 ⇒ 我们**根本不知道**文件里有什么 ⇒ 绝不能用空列表覆盖它
    """


def append(entry: dict):
    """追加一条扫描记录。线程安全 + 跨进程安全（读-改-写全程持锁）。

    读失败（OSError）时**放弃本次写入**并记 error 日志 ——
    读不出来 ≠ 内容为空，用空列表覆盖会把用户的全部历史静默清空。
    """
    entry.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    with _lock:
        fpath = _history_file()
        try:
            with _file_lock(fpath):
                records = _load()
                records.append(entry)
                # 保留最近 500 条
                if len(records) > 500:
                    records = records[-500:]
                _save(records)
        except _HistoryReadError as e:
            logger.error("扫描历史读取失败，本次追加已放弃（不覆盖原文件）: %s", e)
        except Exception as e:
            logger.error("追加扫描历史失败: %s", e)


def list_records(limit: int = 50) -> list:
    """返回最近 limit 条记录，最新的在前。"""
    with _lock:
        try:
            records = _load()
        except _HistoryReadError:
            return []
    return list(reversed(records[-limit:]))


def clear():
    """清空所有历史记录。"""
    with _lock:
        fpath = _history_file()
        try:
            with _file_lock(fpath):
                _save([])
        except Exception as e:
            logger.error("清空扫描历史失败: %s", e)


def _backup_corrupt(fpath: str):
    """
    把损坏的历史文件原文备份成 <name>.corrupt-<时间戳>，返回备份路径。

    同一份损坏内容只备份一次（按 路径+mtime+size 去重），避免反复读取刷出一堆备份。
    备份失败不影响主流程（原文仍在原处，不会被静默清空）。
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
    backup = f"{fpath}.corrupt-{ts}"
    try:
        shutil.copyfile(fpath, backup)
    except OSError as e:
        logger.error("扫描历史损坏备份失败: %s（%s）", fpath, e)
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


def _save(records: list):
    """
    保存扫描历史（唯一临时文件 + 原子替换 + 写后校验）。

    写失败不抛异常，只记 error 日志；原子替换保证失败时旧文件原样保留。
    """
    fpath = _history_file()
    try:
        _atomic_write_json(fpath, records)
    except Exception as e:
        logger.error("扫描历史保存失败: %s", e)
