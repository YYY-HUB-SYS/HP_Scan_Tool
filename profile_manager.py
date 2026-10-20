"""
扫描配置文件管理器 — HP Scan Tool v4.0 — 按设备 IP 保存独立的扫描参数

每个扫描仪（按 IP 标识）可以保存独立的参数配置：
- 分辨率、颜色模式、输出格式、来源
- 切换设备时自动加载对应配置

★ 损坏文件保护（N-4，与 history_manager / preset_manager / page_meta 同一族）
------------------------------------------------------------------
`profiles.json` 损坏（半截写入 / 磁盘满 / 外部改写）时，三条铁律：

1. **读侧照旧降级**：返回 {}，不抛异常（保持既有语义，GUI 不崩）；
2. **先备份原文**：同目录 <name>.corrupt-<时间戳>（时间戳带**微秒** ⇒
   连续两次损坏产生不同文件名，互不覆盖），备份路径经安全检查
   （必须留在应用数据目录内）；
3. **禁止用空状态覆盖**：写路径发现"损坏 / 读不出来"时**拒绝写入**并返回
   False ⇒ 用户其它设备的档案不会被静默清空，原文可从备份恢复。

⚠ "读不出来"（OSError：被杀毒/索引程序独占、PermissionError…）与"文件损坏"
   必须区分开：读不出来 ⇒ 我们根本不知道文件里有什么 ⇒ 绝不能用空档案覆盖它。
"""

import json
import logging
import os
import shutil
import threading
import time
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


def _profile_path():
    """配置文件路径"""
    return os.path.join(_get_app_dir(), "profiles.json")


_lock = threading.Lock()
_profiles_cache = None
_profiles_cache_time: float = 0
# 内存缓存 TTL（E-4）：跨进程写入后，TTL 内读不到、TTL 外能读到。
_PROFILES_CACHE_TTL = 2.0

# 已备份过的损坏文件（路径 + mtime + size），避免反复读取刷出一堆备份
_CORRUPT_BACKED_UP = set()

# ── 读盘状态 ────────────────────────────────────────────────
# 读侧一律降级为 {}（不抛），但**写路径必须区分**这四种状态：
#   missing    文件不存在（首次运行）—— 空档案，写入安全
#   ok         正常读到对象
#   corrupt    读到了但内容损坏（JSON 解析失败 / 编码坏 / 顶层不是对象）
#              —— 原文已备份；覆盖会连同其它设备的档案一起丢掉 ⇒ 写路径拒绝
#   unreadable 读不出来（OSError）—— 根本不知道文件里有什么 ⇒ 绝不用空状态覆盖
_STATE_MISSING = "missing"
_STATE_OK = "ok"
_STATE_CORRUPT = "corrupt"
_STATE_UNREADABLE = "unreadable"

# ── 原子写 + 跨进程锁 ──────────────────────────────────────
# 实现已收口到共享模块 config_io：
#   atomic_write_json —— 唯一临时文件 + 写后校验 + os.replace 原子替换
#   config_write_lock —— msvcrt 跨进程排他锁（同目录 <name>.lock，常驻不删），
#                        **严格版**：拿不到锁抛 ConfigLockTimeout，事务体不执行


def _is_inside_app_dir(path: str) -> bool:
    """
    备份路径安全检查：目标必须留在**应用数据目录**之内
    （生产环境 = %APPDATA%\\HP_Scan_Tool\\）。
    越界（跳出目录 / 不同盘符）一律拒绝。
    """
    try:
        root = os.path.normcase(os.path.abspath(_get_app_dir()))
        target = os.path.normcase(os.path.abspath(path))
        if os.path.commonpath([root, target]) != root:
            return False
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            appdata_abs = os.path.normcase(os.path.abspath(appdata))
            if os.path.commonpath([appdata_abs, root]) != appdata_abs:
                return False
        return True
    except ValueError:
        return False   # 不同盘符


def _backup_corrupt(fpath: str):
    """
    把损坏的设备档案原文备份成 <name>.corrupt-<时间戳>（**同目录**），返回备份路径。

    - 时间戳带**微秒** ⇒ 连续两次损坏产生不同文件名，互不覆盖
    - 同一份损坏内容只备份一次（按 路径+mtime+size 去重）
    - 备份路径经 _is_inside_app_dir 安全检查（不能写到应用数据目录之外）
    - 备份失败不影响主流程（原文仍在原处，不会被静默清空）
    """
    try:
        st = os.stat(fpath)
    except OSError:
        return None
    key = (fpath, st.st_mtime_ns, st.st_size)
    if key in _CORRUPT_BACKED_UP:
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = "%s.corrupt-%s" % (fpath, ts)
    if not _is_inside_app_dir(backup):
        logger.error("设备档案损坏备份被拒绝（路径越出应用数据目录）: %s", backup)
        return None
    _CORRUPT_BACKED_UP.add(key)
    try:
        shutil.copyfile(fpath, backup)
    except OSError as e:
        logger.error("设备档案损坏备份失败: %s（%s）", fpath, e)
        return None
    logger.error("设备档案文件已损坏，原文已备份到: %s", backup)
    return backup


def _read_profiles_state() -> tuple:
    """
    读盘并返回 (档案字典, 状态)。**读侧永不抛异常**（损坏/读失败都降级为 {}）。

    状态见上方 _STATE_* 常量。写路径必须凭状态判断"能不能覆盖原文件"。
    """
    path = _profile_path()
    if not os.path.exists(path):
        return {}, _STATE_MISSING
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("设备档案文件损坏: %s（%s）", path, e)
        _backup_corrupt(path)
        return {}, _STATE_CORRUPT
    except OSError as e:
        logger.error("设备档案文件读取失败: %s（%s）", path, e)
        # 读不出来也要尽力备份（文件被独占时备份可能失败，但不会抛）
        _backup_corrupt(path)
        return {}, _STATE_UNREADABLE
    except Exception as e:
        logger.error("设备档案文件读取失败: %s（%s）", path, e)
        return {}, _STATE_UNREADABLE
    if not isinstance(data, dict):
        logger.error("设备档案文件格式错误（期望对象）: %s", path)
        _backup_corrupt(path)
        return {}, _STATE_CORRUPT
    return data, _STATE_OK


def _read_profiles_from_disk() -> dict:
    """直接读盘（绕过内存缓存）。跨进程锁内用这个拿最新值，避免丢写。

    读侧降级语义：文件不存在 / 损坏 / 读不出来一律返回 {}，**不抛异常**。
    ⚠ 写路径要判断"能不能覆盖原文件"，请用 _read_profiles_state()（带状态）。
    """
    return _read_profiles_state()[0]


def _read_profiles_for_write() -> tuple:
    """
    写路径读盘：返回 (档案字典, 是否可以安全覆盖原文件)。

    - missing / ok  ⇒ True（正常写）
    - corrupt        ⇒ False（原文已备份，但覆盖会丢掉其它设备的档案）
    - unreadable     ⇒ False（不知道文件里有什么，绝不能用空状态覆盖）
    """
    profiles, state = _read_profiles_state()
    if state in (_STATE_CORRUPT, _STATE_UNREADABLE):
        return profiles, False
    return profiles, True


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

    调用方须已持有严格跨进程写锁（读-改-写事务由调用方加锁）。
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
        （文件损坏 / 读不出来时同样返回空字典，不抛异常）
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
        拿不到跨进程锁 ⇒ 原文件保持不变，或写失败，或**原文件损坏/读不出来
        ⇒ 拒绝覆盖**）。绝不"未持锁还写入"（那会覆盖别人的更新 = 丢数据）。

    ★ 锁作用域（终审 I-2）：**跨进程锁在外、进程内锁在内**。
      等跨进程锁（最长 30 秒）时**不持有**进程内锁 ⇒ 同进程其它线程
      （GUI 线程读一次档案）不被连带堵住 ⇒ 界面不再假死 30 秒。
      读-改-写事务体整体仍在进程内锁内 ⇒ 不丢更新。
    """
    if not ip:
        return False
    path = _profile_path()
    try:
        # ── 跨进程锁（**最外层**）：等待期间不持有任何进程内锁 ──
        with config_io.config_write_lock(path):
            # ── 进程内锁：只保护"读盘 → 改 → 写盘"这一小段事务体 ──
            with _lock:
                # 锁内重读磁盘：拿到其他进程刚写的配置，避免丢写
                profiles, can_overwrite = _read_profiles_for_write()
                if not can_overwrite:
                    logger.error(
                        "保存设备档案被拒绝：%s 损坏或读不出来，本次未写入"
                        "（原文件保持不变）。损坏原文已备份为 <同名>.corrupt-<时间戳>，"
                        "如需重建请手动删除原文件后重试。", path)
                    return False
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

    返回本次操作是否成功（False = 拿不到锁/写失败/原文件损坏或读不出来
    ⇒ 本次没生效，原文件不变）。

    ★ 锁作用域同 save_profile：**跨进程锁在外、进程内锁在内**（终审 I-2）。
    """
    if not ip:
        return False
    path = _profile_path()
    try:
        with config_io.config_write_lock(path):
            with _lock:
                profiles, can_overwrite = _read_profiles_for_write()
                if not can_overwrite:
                    logger.error(
                        "删除设备档案被拒绝：%s 损坏或读不出来，本次未删除"
                        "（原文件保持不变）。", path)
                    return False
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

    返回本次操作是否成功（False = 拿不到锁/写失败/原文件读不出来 ⇒
    本次没清空，原文件不变）。

    ★ 原文件**损坏**时仍然清空：损坏原文已在读盘时备份（可恢复），
      而"清空"本身就是用户明确要求的意图；读不出来则不动它。

    ★ 锁作用域同 save_profile：**跨进程锁在外、进程内锁在内**（终审 I-2）。
    """
    path = _profile_path()
    try:
        with config_io.config_write_lock(path):
            with _lock:
                _, state = _read_profiles_state()
                if state == _STATE_UNREADABLE:
                    logger.error(
                        "清空设备档案被拒绝：%s 读不出来，本次未清空"
                        "（原文件保持不变）。", path)
                    return False
                if not _save_profiles({}):
                    return False
    except config_io.ConfigLockTimeout as e:
        logger.error("清除配置文件被跳过（拿不到跨进程锁，本次未写入）: %s（%s）", path, e)
        return False
    except Exception as e:
        logger.error("清除配置文件失败: %s", e)
        return False
    return True
