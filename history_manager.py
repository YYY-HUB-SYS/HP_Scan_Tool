"""
扫描历史管理器 — 轻量 JSON 日志
每次扫描记录元数据到 history.json，支持追加/列表/清空。
"""

import json
import os
import threading
from datetime import datetime

_app_dir = None


def _get_app_dir():
    global _app_dir
    if _app_dir is None:
        import sys
        if getattr(sys, 'frozen', False):
            _app_dir = os.path.dirname(sys.executable)
        else:
            _app_dir = os.path.dirname(os.path.abspath(__file__))
    return _app_dir


HISTORY_FILE = None  # 延迟初始化


def _history_file():
    global HISTORY_FILE
    if HISTORY_FILE is None:
        HISTORY_FILE = os.path.join(_get_app_dir(), "history.json")
    return HISTORY_FILE


_lock = threading.Lock()


def append(entry: dict):
    """追加一条扫描记录。线程安全。"""
    entry.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    with _lock:
        records = _load()
        records.append(entry)
        # 保留最近 500 条
        if len(records) > 500:
            records = records[-500:]
        _save(records)


def list_records(limit: int = 50) -> list:
    """返回最近 limit 条记录，最新的在前。"""
    with _lock:
        records = _load()
    return list(reversed(records[-limit:]))


def clear():
    """清空所有历史记录。"""
    with _lock:
        _save([])


def _load() -> list:
    fpath = _history_file()
    if not os.path.exists(fpath):
        return []
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(records: list):
    fpath = _history_file()
    try:
        tmp = fpath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fpath)
    except OSError:
        pass
