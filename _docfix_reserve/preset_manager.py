"""
曝光预设管理器 — HP Scan Tool v3.3
内置预设 + 用户自定义预设，存储于 presets.json。
"""

import json
import logging
import os

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
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.json")


def load_presets() -> list[dict]:
    """加载全部预设（内置 + 用户自定义）"""
    presets = list(_BUILTIN_PRESETS)
    path = _presets_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, list):
                for p in user:
                    p["builtin"] = False
                    presets.append(p)
        except Exception as e:
            logger.debug("预设加载失败: %s", e)
    return presets


def save_user_preset(preset: dict):
    """保存用户自定义预设（追加或覆盖同名）"""
    path = _presets_path()
    user_presets = _load_user_presets()

    # 覆盖同名
    user_presets = [p for p in user_presets if p["name"] != preset["name"]]
    preset = dict(preset)
    preset.pop("builtin", None)
    user_presets.append(preset)

    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(user_presets, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("预设保存失败: %s", e)


def delete_user_preset(name: str):
    """删除用户自定义预设（内置不可删）"""
    path = _presets_path()
    user_presets = _load_user_presets()
    user_presets = [p for p in user_presets if p["name"] != name]
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(user_presets, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("预设删除失败: %s", e)


def _load_user_presets() -> list[dict]:
    """只加载用户自定义预设"""
    path = _presets_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


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
