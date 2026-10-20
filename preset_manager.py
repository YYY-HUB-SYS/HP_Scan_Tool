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

import copy
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

# ★ 内置预设的**唯一真源**：模块私有，任何调用方都拿不到它的本体。
#   （对外出口 `builtin_presets()` 与旧名 `_BUILTIN_PRESETS` 都只给**深拷贝**）
_BUILTIN_PRESET_TEMPLATE = (
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
)


def builtin_presets() -> list[dict]:
    """
    内置预设的**深拷贝**列表（公开的"只读内置预设"出口）。

    ★ 必须深拷贝到**嵌套层级**（``channel_gains`` 是 list）：
      调用方就地改一下就污染模块级内置预设 —— 终审实测
      ``brightness`` 被改成 15 → 999，之后所有扫描都用错曝光参数。
    ★ 本模块**不 import 任何 Qt**（CLI 也要能用）⇒ 只用返回值。
    """
    return copy.deepcopy(list(_BUILTIN_PRESET_TEMPLATE))


def __getattr__(name):
    """
    模块级 ``__getattr__``（PEP 562）：旧名 ``_BUILTIN_PRESETS`` 现在**每次访问
    都返回一份深拷贝**，调用方再怎么就地改也污染不到内置预设本体。
    """
    if name == "_BUILTIN_PRESETS":
        return builtin_presets()
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


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
    """
    加载全部预设（内置 + 用户自定义）。

    ★ 内置预设以**深拷贝**返回（见 builtin_presets）：调用方就地修改
      不会污染模块级内置预设（终审实测 brightness 15 → 999）。
    ★ 用户预设来自 `_read_user_presets()` —— 每次调用都是**新解析**的
      对象，本来就不共享；这里照样复制一份，让"返回结果"与"文件内容"
      之间没有别名关系。
    """
    presets = builtin_presets()
    user, _ = _read_user_presets()
    for p in user:
        p = copy.deepcopy(p)
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

    返回值（**调用方只能靠它判断成败**）：
      - 成功：返回导入的预设名称（str，非空）
      - 失败：返回 None —— 文件读不了 / 顶层格式不对 / "preset" 不是对象 /
        缺必要字段 / 字段类型不对 / **写盘失败**

    ★ 失败一律"返回 None + 日志"，**不抛异常**：本函数会被 GUI 的槽调用，
      PySide6 ≥ 6.5 对槽内未捕获异常会 qFatal ⇒ 进程 abort。
    ★ 本模块**不 import 任何 Qt**（CLI 也要能用）⇒ 只能用返回值/异常表达成败，
      这里选返回值（既有调用约定与测试都是"None 表示失败"）。
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
    # ★ 类型校验：以前这里直接 preset.keys() ⇒ "preset" 是字符串时抛
    #   AttributeError（未捕获 ⇒ Qt 槽里 qFatal ⇒ 进程崩），而且调用方
    #   无从知道导入到底成没成。
    if not isinstance(preset, dict):
        logger.error("预设导入失败：preset 字段必须是对象，实际是 %s",
                     type(preset).__name__)
        return None

    required_keys = {"name", "mode"}
    if not required_keys.issubset(preset.keys()):
        logger.debug("预设文件缺少必要字段")
        return None

    name = preset["name"]
    mode = preset["mode"]
    if not isinstance(name, str) or not name.strip():
        logger.error("预设导入失败：name 必须是非空字符串，实际是 %r", name)
        return None
    if not isinstance(mode, str) or not mode.strip():
        logger.error("预设导入失败：mode 必须是非空字符串，实际是 %r", mode)
        return None

    # ★ 必须接住写盘结果：写失败时**不能**照样返回预设名
    #   （以前返回值被丢弃 ⇒ 用户点了"导入"却以为成功了）。
    if not save_user_preset(preset):
        logger.error("预设导入失败：写入预设文件失败（%s）", name)
        return None
    return name
