"""
统一命名 — HP Scan Tool v4.0（**零依赖**：只用标准库 uuid / datetime）

★ 为什么单独一个模块（本轮收口动作的核心）
--------------------------------------------------------------------------
仓库里有**三个**入口会产出用户可见的产物名 / job_id：

    ① ``scan_coordinator.new_output_name`` / ``new_job_id``（直存 · 协调器）
    ② ``cli._timestamped_name``（命令行）
    ③ ``hp_scan_gui_pyside._timestamped_output_name`` / ``_timestamped_job_id``

2026-10-21 之前它们是**两种形状**（两条并行修复线各选了一种）：

    ① ``HP_Scan_20261021_065432_e4404e9d.jpg``       （秒级时间戳 + 8 位 hex）
    ②③ ``HP_Scan_20261021_045213_123456_c3f19a.jpg`` （秒 + **微秒** + 1 字母 5 hex）

⇒ 用户在 GUI 里会看到**两种形状**（直存走 ①，会话落盘走 ③）。

★ 但三处**不能互相 import**：
    - ``cli.py`` **不得** import Qt / GUI / 协调器（守护用例 ``tests/test_cli_no_qt.py``）
    - ``scan_coordinator.py`` **不得** import GUI（Qt 依赖只在这一侧）
⇒ 形态只能收敛到**一个零依赖模块**，三处各自 import 它 ⇒ **只有一份实现**。

★ 统一形态（主控拍板）
--------------------------------------------------------------------------
    唯一成分   = **1 位字母（a–p）+ 7 位十六进制**（8 字符）
    job_id     = ``{YYYYmmdd}_{HHMMSS}_{唯一成分}``
    输出文件名  = ``{前缀}_{YYYYmmdd}_{HHMMSS}_{唯一成分}.{扩展名}``

    例：``20261021_065432_a3f19a2c``
        ``HP_Scan_20261021_065432_a3f19a2c.jpg``

★ 为什么**去掉微秒段**（虚假精度）
--------------------------------------------------------------------------
本机 Windows **系统时钟粒度实测 15.625 ms**
（``time.get_clock_info('time').resolution == 0.015625``）
⇒ 同一 tick 内两次 ``datetime.now().strftime("%f")`` 的结果 **99.5% 完全相同**
⇒ 那 6 位微秒在同 tick 内**本来就一样、没有额外信息量**
⇒ 保留它是**虚假精度**（会让人误以为精确到微秒）⇒ 删掉。
真正的唯一性必须来自**随机成分**（uuid4 ⇒ 与时钟分辨率完全无关）。

★ 为什么唯一成分**首字符固定是字母**（a–p）
--------------------------------------------------------------------------
产物名已有**分组编号**规则（``{base}_001.jpg`` / ``{base}_{组号}_{组内号}.ext``）
⇒ 唯一成分若全是数字，会与数字段**混淆**（实测纯六进制随机约 6% 概率全数字）
⇒ 首字符固定字母 ⇒ 永远不会长得像组号。
随机空间 = 16 种首字母 × 16^7 ≈ **4.29e9** ⇒ 碰撞概率可忽略。

★ 契约（一个字都不能破）
--------------------------------------------------------------------------
- 扩展名不变；单页 / 多页规则不变
- ``save_manager.build_output_name`` 的分组编号规则**一字未改**
  （``{base}_001.jpg`` / ``{base}_{组号}_{组内号}.ext``）
  ⇒ 唯一成分落在**基名**里
- job_id 只含 ``[0-9a-z_]`` ⇒ 可安全作目录名 / page_meta 的 key

★ 为什么时钟由**调用方**传入（``now=datetime.now()``）
--------------------------------------------------------------------------
时间戳**格式**只在本模块定义一次（防形态漂移），
而**取时钟**的动作留在各入口自己的模块里 ⇒ 各模块的模块级 ``datetime``
仍可被测试**冻住**（``monkeypatch.setattr(module, "datetime", ...)`），
护栏用例才能稳定复现「同一 tick 内两次取名」。
"""

import uuid
from datetime import datetime

#: 时间戳格式：**秒级**（不含微秒 —— 见模块 docstring 的「虚假精度」）
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

#: 唯一成分：1 位字母（a–p）+ 7 位十六进制
TOKEN_LETTER_COUNT = 16
TOKEN_HEX_LEN = 7
TOKEN_LEN = TOKEN_HEX_LEN + 1


def unique_token() -> str:
    """
    唯一成分：**1 位字母（a–p）+ 7 位十六进制**（8 字符）。

    ★ 来源是 ``uuid4()``（操作系统随机源：Windows 下是 CryptGenRandom）
      ⇒ **与时钟分辨率完全无关** ⇒ 同一 tick 内连取 N 次也不会相同。
    ★ 首字符由 uuid 的第 1 个十六进制位映射而来（0–f ⇒ a–p），
      因此它**永远不会**长得像分组编号（``_001``）。
    """
    raw = uuid.uuid4().hex[:TOKEN_LEN]
    return (chr(ord("a") + int(raw[0], 16) % TOKEN_LETTER_COUNT)
            + raw[1:TOKEN_LEN])


def timestamp(now=None) -> str:
    """秒级时间戳：``{YYYYmmdd}_{HHMMSS}``。"""
    if now is None:
        now = datetime.now()
    return now.strftime(TIMESTAMP_FORMAT)


def build_output_name(prefix: str, ext: str, now=None) -> str:
    """
    输出文件名：``{前缀}_{YYYYmmdd}_{HHMMSS}_{唯一成分}.{扩展名}``。

    ★ ``now`` 由调用方传入（``datetime.now()``）⇒ 取时钟留在调用方，
      时间戳**格式**只在本模块定义一次。
    """
    return "%s_%s_%s.%s" % (prefix, timestamp(now), unique_token(), ext)


def build_job_id(now=None) -> str:
    """
    job_id：``{YYYYmmdd}_{HHMMSS}_{唯一成分}``。

    ★ 只含 ``[0-9a-z_]`` ⇒ 可安全用作**缓存目录名** / page_meta 的 key。
    """
    return "%s_%s" % (timestamp(now), unique_token())
