"""
扫描配置文件管理器 — 按设备 IP 保存独立的扫描参数

每个扫描仪（按 IP 标识）可以保存独立的参数配置：
- 分辨率、颜色模式、输出格式、来源
- 切换设备时自动加载对应配置
"""

import json
import logging
import os
import sys
import threading

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


def _load_profiles():
    """加载所有配置文件"""
    global _profiles_cache
    if _profiles_cache is not None:
        return _profiles_cache

    path = _profile_path()
    if not os.path.exists(path):
        _profiles_cache = {}
        return _profiles_cache

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                _profiles_cache = data
                return _profiles_cache
    except Exception as e:
        logging.warning("加载配置文件失败: %s", e)

    _profiles_cache = {}
    return _profiles_cache


def _save_profiles(profiles):
    """保存所有配置文件"""
    global _profiles_cache
    _profiles_cache = profiles
    path = _profile_path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(profiles, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logging.error("保存配置文件失败: %s", e)


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
        profiles = _load_profiles()
        profiles[ip] = params.copy()
        _save_profiles(profiles)


def delete_profile(ip: str):
    """删除指定设备的配置文件"""
    if not ip:
        return
    with _lock:
        profiles = _load_profiles()
        if ip in profiles:
            del profiles[ip]
            _save_profiles(profiles)


def get_all_profiles() -> dict:
    """获取所有配置文件"""
    with _lock:
        return _load_profiles().copy()


def clear_all():
    """清除所有配置文件"""
    with _lock:
        _save_profiles({})
