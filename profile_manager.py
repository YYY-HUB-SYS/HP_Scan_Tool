"""
扫描配置文件管理器 — HP Scan Tool v4.0 — 按设备 IP 保存独立的扫描参数

每个扫描仪（按 IP 标识）可以保存独立的参数配置：
- 分辨率、颜色模式、输出格式、来源
- 切换设备时自动加载对应配置
"""

import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time

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


def _profile_path():
    """配置文件路径"""
    return os.path.join(_get_app_dir(), "profiles.json")


_lock = threading.Lock()
_profiles_cache = None

# ── 原子写 + 跨进程锁 ──────────────────────────────────────
# 配置写入约定（cache_manager / history_manager / profile_manager 三处统一）：
#   1) 临时文件名唯一（mkstemp）——并发写不会互相覆盖
#   2) 替换前先读回校验 JSON 可解析，校验不过就不替换——旧文件原样保留
#   3) 同目录 <name>.lock 跨进程排他锁，锁住整个"读-改-写"事务
#   4) 写失败记 error 日志，不抛异常（避免 GUI 因配置写失败而崩）

_LOCK_WAIT_SECONDS = 2.0   # 拿不到跨进程锁时的最长等待时间
_LOCK_POLL_SECONDS = 0.05  # 重试间隔


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


def _read_profiles_from_disk() -> dict:
    """直接读盘（绕过内存缓存）。跨进程锁内用这个拿最新值，避免丢写。"""
    path = _profile_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning("加载配置文件失败: %s", e)
    return {}


def _load_profiles():
    """加载所有配置文件"""
    global _profiles_cache
    if _profiles_cache is not None:
        return _profiles_cache

    _profiles_cache = _read_profiles_from_disk()
    return _profiles_cache


def _save_profiles(profiles):
    """
    保存所有配置文件（唯一临时文件 + 原子替换）。写失败不抛异常，只记日志。

    调用方须已持有 _file_lock（跨进程读-改-写事务由调用方加锁）。
    """
    global _profiles_cache
    path = _profile_path()
    try:
        _atomic_write_json(path, profiles)
    except Exception as e:
        logger.error("保存配置文件失败: %s", e)
        return
    _profiles_cache = profiles


def get_profile(ip: str) -> dict:
    """
    获取指定设备的配置文件。

    Args:
        ip: 设备 IP 地址

    Returns:
        配置字典，如果不存在返回空字典
    """
    if not ip:
        return {}
    with _lock:
        profiles = _load_profiles()
        return profiles.get(ip, {}).copy()


def save_profile(ip: str, params: dict):
    """
    保存指定设备的配置文件。

    Args:
        ip: 设备 IP 地址
        params: 参数字典（分辨率、颜色、格式、来源等）
    """
    if not ip:
        return
    with _lock:
        path = _profile_path()
        try:
            with _file_lock(path):
                # 锁内重读磁盘：拿到其他进程刚写的配置，避免丢写
                profiles = _read_profiles_from_disk()
                profiles[ip] = params.copy()
                _save_profiles(profiles)
        except Exception as e:
            logger.error("保存配置文件失败: %s", e)


def delete_profile(ip: str):
    """删除指定设备的配置文件"""
    if not ip:
        return
    with _lock:
        path = _profile_path()
        try:
            with _file_lock(path):
                profiles = _read_profiles_from_disk()
                if ip in profiles:
                    del profiles[ip]
                    _save_profiles(profiles)
        except Exception as e:
            logger.error("删除配置文件失败: %s", e)


def get_all_profiles() -> dict:
    """获取所有配置文件"""
    with _lock:
        return _load_profiles().copy()


def clear_all():
    """清除所有配置文件"""
    with _lock:
        path = _profile_path()
        try:
            with _file_lock(path):
                _save_profiles({})
        except Exception as e:
            logger.error("清除配置文件失败: %s", e)
