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
  5) 锁分两档：
     - config_lock（默认 required=False）：拿不到锁时 yield acquired(False)，
       调用方自行决定要不要写——**老调用点靠这个不变量**，
       改成"抛异常"会给未同步的模块凭空引入异常逃逸路径。
     - config_write_lock（= required=True）：拿不到锁就抛 ConfigLockTimeout，
       事务体不执行 ⇒ 绝不"未持锁还写入"（丢更新的根因）。

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

_LOCK_WAIT_SECONDS = 30.0  # 拿不到跨进程锁时的最长等待时间
_LOCK_POLL_SECONDS = 0.05  # 重试间隔


class ConfigLockTimeout(RuntimeError):
    """拿不到跨进程锁（等待超时）。

    仅由 ``config_lock(required=True)`` / ``config_write_lock`` 抛出，
    语义是"本次写事务没有发生"——调用方要么放弃本次写入，要么重试整个
    "读-改-写"事务，**绝不能**在未持锁的情况下继续写。
    """


def config_dir() -> str:
    """配置目录：%APPDATA%\\HP_Scan_Tool\\（Windows 标准应用数据目录）"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        appdata = os.path.expanduser("~")
    path = os.path.join(appdata, "HP_Scan_Tool")
    os.makedirs(path, exist_ok=True)
    return path


@contextlib.contextmanager
def config_lock(path: str, timeout: float = None, required: bool = False):
    """
    跨进程排他锁（Windows: msvcrt.locking）。

    - 锁文件 <path>.lock 常驻磁盘，不删除；只锁它的第 1 个字节
    - 多个进程共用同一把锁 ⇒ 写事务串行化
    - timeout=None 时用 _LOCK_WAIT_SECONDS（30 秒）；timeout 覆盖等待上限
    - required=False（默认，**保持既有行为不变**）：等待超时后 yield acquired(False)，
      由调用方自行决定要不要写（波次 2 尚未收紧的老调用点就是这种语义）
    - required=True：等待超时后抛 ConfigLockTimeout，事务体**不会执行**
      ⇒ 绝不会发生"未持锁还写入"导致的丢更新
    """
    if msvcrt is None:  # 非 Windows：无跨进程锁
        if required:
            # 没有跨进程锁可用时放行（否则非 Windows 平台所有写入都会失败）
            logger.warning("当前平台无跨进程锁支持，放行写事务: %s", path)
            yield True
        else:
            yield False
        return

    wait_seconds = _LOCK_WAIT_SECONDS if timeout is None else float(timeout)
    lock_path = path + ".lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, flags)
    acquired = False
    try:
        # ★ 锁文件初始化必须容错（F-1）：
        #   锁文件**首次创建**时，两个进程可能同时看到 size==0 并各写一次；
        #   而 Windows 的字节区间锁对**写操作**是强制的——先拿到锁的那一方
        #   会让后写者收到 PermissionError。异常一旦逃出上下文管理器，
        #   整条"读-改-写"事务就被放弃（扫描历史 / 设备配置 / 页元数据静默丢失）。
        #   锁文件的长度不影响排他性（Windows 允许锁定超出 EOF 的字节区间），
        #   所以写失败直接忽略即可。
        if os.fstat(fd).st_size == 0:
            try:
                os.write(fd, b"\0")
            except OSError:
                pass
        deadline = time.monotonic() + wait_seconds
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
        if acquired and os.fstat(fd).st_size == 0:
            # 兜底：上面那次初始化写可能被另一个句柄挡掉了，拿到锁后再补一次
            try:
                os.write(fd, b"\0")
            except OSError:
                pass
        if not acquired:
            if required:
                raise ConfigLockTimeout(
                    f"等待跨进程锁超时（{wait_seconds:.1f} 秒）: {lock_path}")
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


@contextlib.contextmanager
def config_write_lock(path: str, timeout: float = None):
    """
    严格版写事务锁：拿不到跨进程锁就抛 ConfigLockTimeout。

    与 config_lock(required=False) 的区别：
      - 事务体**不会**在未持锁的情况下执行（杜绝"未持锁还写入"→ 丢更新）
      - 拿不到锁时抛 ConfigLockTimeout，由调用方决定跳过本次写入还是重试事务
    """
    with config_lock(path, timeout=timeout, required=True) as acquired:
        yield acquired


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
