"""
扫描历史管理器 — HP Scan Tool v4.0 — 轻量 JSON 日志
每次扫描记录元数据到 history.json，支持追加/列表/清空。
"""

import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime

try:
    import msvcrt
except ImportError:  # pragma: no cover - 非 Windows 环境降级为无跨进程锁
    msvcrt = None

logger = logging.getLogger(__name__)

_app_dir = None


def _get_app_dir():
    global _app_dir
    if _app_dir is None:
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            appdata = os.path.expanduser("~")
        _app_dir = os.path.join(appdata, "HP_Scan_Tool")
        os.makedirs(_app_dir, exist_ok=True)
    return _app_dir


HISTORY_FILE = None  # 延迟初始化


def _history_file():
    global HISTORY_FILE
    if HISTORY_FILE is None:
        HISTORY_FILE = os.path.join(_get_app_dir(), "history.json")
    return HISTORY_FILE


_lock = threading.Lock()

# ── 原子写 + 跨进程锁 ──────────────────────────────────────
# 配置写入约定（cache_manager / history_manager / profile_manager 三处统一）：
#   1) 临时文件名唯一（mkstemp）——并发写不会互相覆盖
#   2) 替换前先读回校验 JSON 可解析，校验不过就不替换——旧文件原样保留
#   3) 同目录 <name>.lock 跨进程排他锁，锁住整个"读-改-写"事务
#   4) 写失败记 error 日志，不抛异常（避免 GUI 因配置写失败而崩）

_LOCK_WAIT_SECONDS = 2.0   # 拿不到跨进程锁时的最长等待时间
_LOCK_POLL_SECONDS = 0.05  # 重试间隔

# 已备份过的损坏文件（路径 + mtime + size），避免反复读取刷出一堆备份
_CORRUPT_BACKED_UP = set()


@contextlib.contextmanager
def _file_lock(path: str):
    """
    跨进程排他锁（Windows: msvcrt.locking）。

    - 锁文件 <path>.lock 常驻磁盘，不删除；只锁它的第 1 个字节
    - 多个进程共用同一把锁 ⇒ 写事务串行化
    - 拿不到锁时最多等 _LOCK_WAIT_SECONDS；仍失败则放行（记日志），不跳过写入
    """
    if msvcrt is None:  # 非 Windows：无跨进程锁，直接放行
        yield False
        return

    lock_path = path + ".lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, flags)
    acquired = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
        if not acquired:
            logger.warning("跨进程锁等待超时，继续写入: %s", lock_path)
        yield acquired
    finally:
        if acquired:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def _atomic_write_json(path: str, data) -> None:
    """
    唯一临时文件 + os.replace 原子替换写 JSON。

    - 临时名唯一（mkstemp），并发写互不覆盖
    - 替换前先读回校验 JSON 可解析，校验不过则抛错、不替换（旧文件保持不变）
    - 异常路径清理临时文件
    """
    directory = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=basename + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp_path, "r", encoding="utf-8") as f:
            json.load(f)  # 校验：解析不了就不替换
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def append(entry: dict):
    """追加一条扫描记录。线程安全 + 跨进程安全（读-改-写全程持锁）。"""
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
        except Exception as e:
            logger.error("追加扫描历史失败: %s", e)


def list_records(limit: int = 50) -> list:
    """返回最近 limit 条记录，最新的在前。"""
    with _lock:
        records = _load()
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

    区分两种"读不到记录"：
    - 文件不存在 → 正常首次运行，返回 []
    - 文件损坏（解析失败 / 编码乱码 / 顶层不是数组）→ 备份原文 + 记 error 日志，
      返回 []。原文不会丢：损坏内容留在 <name>.corrupt-<时间戳> 备份文件里。
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
        return []
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
