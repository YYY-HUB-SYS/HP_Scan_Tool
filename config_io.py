"""
配置 I/O 共享层 — HP Scan Tool v4.0

集中放置配置文件的「原子写 + 跨进程锁」helper，供
cache_manager / history_manager / profile_manager 共用
（原先这三处各存一份同名同构的副本）。

配置写入约定（三模块统一）：
  1) 临时文件名唯一（mkstemp）——并发写不会互相覆盖
  2) 替换前先读回校验 JSON 可解析，校验不过就不替换——旧文件原样保留
  3) 同目录 <name>.lock 跨进程排他锁，锁住整个"读-改-写"事务
  4) 写失败记 error 日志，不抛异常（避免 GUI 因配置写失败而崩）
     —— 记日志与吞异常由各调用模块在自己的 logger 上完成，
        本模块只提供底层原语（atomic_write_json 失败时照旧抛异常）。

本模块只做 I/O，不依赖任何 GUI / 业务模块。
"""

import contextlib
import json
import logging
import os
import tempfile
import time

try:
    import msvcrt
except ImportError:  # pragma: no cover - 非 Windows 环境降级为无跨进程锁
    msvcrt = None

logger = logging.getLogger(__name__)

_LOCK_WAIT_SECONDS = 2.0   # 拿不到跨进程锁时的最长等待时间
_LOCK_POLL_SECONDS = 0.05  # 重试间隔


def config_dir() -> str:
    """配置目录：%APPDATA%\\HP_Scan_Tool\\（Windows 标准应用数据目录）"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        appdata = os.path.expanduser("~")
    path = os.path.join(appdata, "HP_Scan_Tool")
    os.makedirs(path, exist_ok=True)
    return path


@contextlib.contextmanager
def config_lock(path: str):
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


def atomic_write_json(path: str, data) -> None:
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
            json.dump(data, f, indent=2, ensure_ascii=False)
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
