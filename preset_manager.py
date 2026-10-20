"""
曝光预设管理器 — HP Scan Tool v3.3
内置预设 + 用户自定义预设，存储于 presets.json。

并发与损坏约定（v4.0）：
  1) 读-改-写全程在**同一个事务**内（config_io.config_write_lock + atomic_write_json），
     并发保存不再互相覆盖（旧实现固定临时名 + 无锁，实测 8 线程 × 25 次
     并发存 200 个预设只剩 5 个）。
  2) 预设文件损坏时：记 error 日志 + **备份原文**（与 history_manager 同风格），
     并且**禁止**用空列表覆盖原文件（否则用户预设永久丢失）。
"""

import json
import logging
import os
import shutil
from datetime import datetime

import config_io

logger = logging.getLogger(__name__)

# 预设数据结构:
# {
#   "name": str,
#   "mode": "off" | "auto" | "manual",
#   "brightness": int, "contrast": int,
#   "gamma": float, "shadows": int, "highlights": int,
#   "channel_gains": [float, float, float],
#   "builtin": bool  (True=内置不可删除)
# }

_BUILTIN_PRESETS = [
    {
        "name": "文字增强",
        "mode": "manual",
        "brightness": 15, "contrast": 40,
        "gamma": 0.8, "shadows": -20, "highlights": 10,
        "channel_gains": [1.0, 1.0, 1.0],
        "builtin": True,
    },
    {
        "name": "照片还原",
        "mode": "manual",
        "brightness": 5, "contrast": 10,
        "gamma": 1.2, "shadows": 15, "highlights": -10,
        "channel_gains": [1.0, 1.0, 1.0],
        "builtin": True,
    },
    {
        "name": "去灰底",
        "mode": "auto",
        "brightness": 0, "contrast": 0,
        "gamma": 1.0, "shadows": 0, "highlights": 0,
        "channel_gains": [1.0, 1.0, 1.0],
        "builtin": True,
    },
    {
        "name": "旧文件修复",
        "mode": "manual",
        "brightness": 20, "contrast": 30,
        "gamma": 0.9, "shadows": -30, "highlights": 20,
        "channel_gains": [0.95, 1.0, 1.1],  # 降红增蓝，去黄
        "builtin": True,
    },
]


def _presets_path() -> str:
    """预设文件路径：%APPDATA%\\HP_Scan_Tool\\presets.json（与其它配置一致）"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        appdata = os.path.expanduser("~")
    app_dir = os.path.join(appdata, "HP_Scan_Tool")
    os.makedirs(app_dir, exist_ok=True)
    return os.path.join(app_dir, "presets.json")


# 已备份过的损坏文件（路径 + mtime + size），避免反复读取刷出一堆备份
_CORRUPT_BACKED_UP = set()


def _backup_corrupt(fpath: str):
    """
    把损坏的预设文件原文备份成 <name>.corrupt-<时间戳>，返回备份路径。

    与 history_manager._backup_corrupt 同一套命名与去重规则：同一份损坏内容
    只备份一次；备份失败不影响主流程（原文仍在原处，不会被静默清空）。
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
        logger.error("预设文件损坏备份失败: %s（%s）", fpath, e)
        return None
    logger.error("预设文件已损坏，原文已备份到: %s", backup)
    return backup


def _read_user_presets() -> tuple[list[dict], bool]:
    """
    读用户自定义预设文件，返回 (预设列表, 是否**可以安全覆盖**原文件)。

    - 文件不存在 / 是空文件 → ([], True)：没有任何数据可丢
    - 文件损坏（JSON 解析失败 / 顶层不是数组）→ 记 error 日志 + **备份原文**，
      返回 ([], False)：调用方**禁止**用空列表覆盖原文件，
      否则用户预设会被静默清空、永久丢失。
    """
    path = _presets_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return [], True
    except UnicodeDecodeError as e:
        logger.error("预设文件编码损坏: %s（%s）", path, e)
        _backup_corrupt(path)
        return [], False
    except OSError as e:
        logger.error("预设文件读取失败: %s（%s）", path, e)
        return [], False
    if not raw.strip():
        return [], True
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("预设文件损坏: %s（%s）", path, e)
        _backup_corrupt(path)
        return [], False
    if not isinstance(data, list):
        logger.error("预设文件格式错误（期望数组）: %s", path)
        _backup_corrupt(path)
        return [], False
    return data, True


def load_presets() -> list[dict]:
    """加载全部预设（内置 + 用户自定义）"""
    presets = list(_BUILTIN_PRESETS)
    user, _ = _read_user_presets()
    for p in user:
        p["builtin"] = False
        presets.append(p)
    return presets


def save_user_preset(preset: dict) -> bool:
    """
    保存用户自定义预设（追加或覆盖同名）。

    读-改-写在同一事务内（跨进程锁 + 原子替换），并发保存不丢更新。
    返回是否真的写入（False = 拿不到锁，或原文件损坏已跳过）。
    """
    path = _presets_path()
    preset = dict(preset)
    preset.pop("builtin", None)

    try:
        with config_io.config_write_lock(path):
            user_presets, overwritable = _read_user_presets()
            if not overwritable:
                logger.error("预设文件已损坏，已跳过保存（原文与备份均保留）: %s", path)
                return False
            # 覆盖同名
            user_presets = [p for p in user_presets
                            if p.get("name") != preset.get("name")]
            user_presets.append(preset)
            config_io.atomic_write_json(path, user_presets)
    except config_io.ConfigLockTimeout as e:
        logger.error("预设保存被跳过（拿不到跨进程锁）: %s（%s）", path, e)
        return False
    except Exception as e:
        logger.error("预设保存失败: %s", e)
        return False
    return True


def delete_user_preset(name: str) -> bool:
    """删除用户自定义预设（内置不可删）。返回是否真的写入。"""
    path = _presets_path()
    try:
        with config_io.config_write_lock(path):
            user_presets, overwritable = _read_user_presets()
            if not overwritable:
                logger.error("预设文件已损坏，已跳过删除（原文与备份均保留）: %s", path)
                return False
            user_presets = [p for p in user_presets if p.get("name") != name]
            config_io.atomic_write_json(path, user_presets)
    except config_io.ConfigLockTimeout as e:
        logger.error("预设删除被跳过（拿不到跨进程锁）: %s（%s）", path, e)
        return False
    except Exception as e:
        logger.error("预设删除失败: %s", e)
        return False
    return True


def export_preset(name: str, output_path: str) -> bool:
    """
    导出指定预设为独立 JSON 文件（用于分享）。
    返回 True 表示成功。
    """
    presets = load_presets()
    target = None
    for p in presets:
        if p["name"] == name:
            target = p
            break
    if not target:
        return False

    export_data = {
        "version": 1,
        "preset": {k: v for k, v in target.items() if k != "builtin"},
    }
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.debug("预设导出失败: %s", e)
        return False


def import_preset(file_path: str) -> str | None:
    """
    从 JSON 文件导入预设。
    返回导入的预设名称，失败返回 None。
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.debug("预设导入读取失败: %s", e)
        return None

    if not isinstance(data, dict) or "preset" not in data:
        logger.debug("预设文件格式无效")
        return None

    preset = data["preset"]
    required_keys = {"name", "mode"}
    if not required_keys.issubset(preset.keys()):
        logger.debug("预设文件缺少必要字段")
        return None

    name = preset["name"]
    save_user_preset(preset)
    return name
