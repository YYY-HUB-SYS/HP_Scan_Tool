"""
扫描配置文件管理器 — HP Scan Tool v4.0 — 按设备 IP 保存独立的扫描参数

每个扫描仪（按 IP 标识）可以保存独立的参数配置：
- 分辨率、颜色模式、输出格式、来源
- 切换设备时自动加载对应配置
"""

import json
import logging
import os
import sys
import threading
import time

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


def _profile_path():
    """配置文件路径"""
    return os.path.join(_get_app_dir(), "profiles.json")


_lock = threading.Lock()
_profiles_cache = None
_profiles_cache_time: float = 0
# 内存缓存 TTL（E-4）：跨进程写入后，TTL 内读不到、TTL 外能读到。
_PROFILES_CACHE_TTL = 2.0

# ── 原子写 + 跨进程锁 ──────────────────────────────────────
# 实现已收口到共享模块 config_io：
#   atomic_write_json —— 唯一临时文件 + 写后校验 + os.replace 原子替换
#   config_write_lock —— msvcrt 跨进程排他锁（同目录 <name>.lock，常驻不删），
#                        **严格版**：拿不到锁抛 ConfigLockTimeout，事务体不执行


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
    """
    加载所有配置文件（带**短 TTL** 的内存缓存，E-4）。

    旧实现只在首次加载时读盘，跨进程写入永远读不到（get_profile /
    get_all_profiles）。现在：TTL 内复用内存缓存，TTL 过期重新读盘 ——
    TTL 语义：**TTL 内的外部改动读不到**（缓存命中），**TTL 外的外部改动
    一定读得到**（重新读盘）。写路径（save_profile / delete_profile /
    clear_all）锁内重读盘 + 写完立即刷新缓存，不受 TTL 影响。
    """
    global _profiles_cache, _profiles_cache_time
    now = time.time()
    if (_profiles_cache is not None
            and (now - _profiles_cache_time) < _PROFILES_CACHE_TTL):
        return _profiles_cache

    _profiles_cache = _read_profiles_from_disk()
    _profiles_cache_time = now
    return _profiles_cache


def _save_profiles(profiles) -> bool:
    """
    保存所有配置文件（唯一临时文件 + 原子替换）。写失败不抛异常，只记日志。

    调用方须已持有 config_io.config_write_lock（跨进程读-改-写事务由调用方加锁）。
    返回是否真的写入（False = 写失败）。
    """
    global _profiles_cache, _profiles_cache_time
    path = _profile_path()
    try:
        _atomic_write_json(path, profiles)
    except Exception as e:
        logger.error("保存配置文件失败: %s", e)
        return False
    _profiles_cache = profiles
    _profiles_cache_time = time.time()
    return True


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


def save_profile(ip: str, params: dict) -> bool:
    """
    保存指定设备的配置文件。

    Args:
        ip: 设备 IP 地址
        params: 参数字典（分辨率、颜色、格式、来源等）

    Returns:
        本次操作是否成功（True = 目标状态已落盘；False = 本次**没生效**：
        拿不到跨进程锁 ⇒ 原文件保持不变，或写失败）。绝不"未持锁还写入"
        （那会覆盖别人的更新 = 丢数据）。
    """
    if not ip:
        return False
    with _lock:
        path = _profile_path()
        try:
            with config_io.config_write_lock(path):
                # 锁内重读磁盘：拿到其他进程刚写的配置，避免丢写
                profiles = _read_profiles_from_disk()
                profiles[ip] = params.copy()
                if not _save_profiles(profiles):
                    return False
        except config_io.ConfigLockTimeout as e:
            logger.error("保存配置文件被跳过（拿不到跨进程锁，本次未写入）: %s（%s）", path, e)
            return False
        except Exception as e:
            logger.error("保存配置文件失败: %s", e)
            return False
        return True


def delete_profile(ip: str) -> bool:
    """删除指定设备的配置文件。

    返回本次操作是否成功（False = 拿不到锁/写失败 ⇒ 本次没生效，原文件不变）。
    """
    if not ip:
        return False
    with _lock:
        path = _profile_path()
        try:
            with config_io.config_write_lock(path):
                profiles = _read_profiles_from_disk()
                if ip in profiles:
                    del profiles[ip]
                    if not _save_profiles(profiles):
                        return False
        except config_io.ConfigLockTimeout as e:
            logger.error("删除配置文件被跳过（拿不到跨进程锁，本次未写入）: %s（%s）", path, e)
            return False
        except Exception as e:
            logger.error("删除配置文件失败: %s", e)
            return False
        return True


def get_all_profiles() -> dict:
    """获取所有配置文件"""
    with _lock:
        return _load_profiles().copy()


def clear_all() -> bool:
    """清除所有配置文件。

    返回本次操作是否成功（False = 拿不到锁/写失败 ⇒ 本次没清空，原文件不变）。
    """
    with _lock:
        path = _profile_path()
        try:
            with config_io.config_write_lock(path):
                if not _save_profiles({}):
                    return False
        except config_io.ConfigLockTimeout as e:
            logger.error("清除配置文件被跳过（拿不到跨进程锁，本次未写入）: %s（%s）", path, e)
            return False
        except Exception as e:
            logger.error("清除配置文件失败: %s", e)
            return False
        return True
