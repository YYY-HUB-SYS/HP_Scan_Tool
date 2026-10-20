"""
图像曝光后处理 — HP Scan Tool v3.3
从 escl_engine 提取的纯图像处理模块，无网络/协议依赖。
auto 模式: 高斯模糊除法文档增强（去灰底 + 均匀化光照）
manual 模式: channel_gains → shadows/highlights → gamma → brightness/contrast
"""

import io
import logging
import math

from PIL import Image

logger = logging.getLogger(__name__)

# ── 曝光参数的合法域 ──────────────────────────────────────────────────
# 与 GUI 滑块范围一致（hp_scan_gui_pyside._SLIDER_SPECS）：
#   明暗/对比度/阴影/高光 -100~100、gamma 0.1~3.0、通道增益 0.5~1.5。
# 超出合法域的输入**回落默认值**，不抛异常 —— 预设 JSON 是用户可编辑的，
# 一个坏数字不该让整批保存整批失败。
PARAM_MIN = -100
PARAM_MAX = 100
GAMMA_MIN = 0.1
GAMMA_MAX = 3.0
DEFAULT_GAMMA = 1.0
DEFAULT_GAINS = (1.0, 1.0, 1.0)


def sanitize_number(value, default, lo=None, hi=None, name="参数") -> float:
    """
    把外部数值安全地转成 float。

    规则（**回落**，不抛）：
      - None / 布尔 / 非数字字符串 / NaN / Inf ⇒ default
      - 数字字符串（"12" / "1.5"）⇒ 正常转换（预设 JSON 里可能是字符串）
      - 超出合法域 [lo, hi] ⇒ default（默认值本身就是"什么都不做"）

    仍然抛 TypeError 的唯一情况：**类型完全不对**（dict / list / 自定义对象）——
    那是内部契约被破坏，不是外部脏数据，不该被静默吞掉。
    """
    if value is None or isinstance(value, bool):
        return float(default)
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return float(default)
    else:
        raise TypeError(f"{name} 的类型不对: {type(value).__name__}")
    if not math.isfinite(number):
        return float(default)
    if lo is not None and number < lo:
        return float(default)
    if hi is not None and number > hi:
        return float(default)
    return number


def sanitize_int(value, default=0, lo=None, hi=None, name="参数") -> int:
    """sanitize_number 的整数版（截断，与旧的 int(...) 语义一致）。"""
    return int(sanitize_number(value, default, lo, hi, name))


def sanitize_gains(value, default=DEFAULT_GAINS) -> tuple:
    """
    通道增益必须是**恰好 3 个有限数字**；否则整组回落默认值。

    长度不对（2 个 / 4 个）曾经让 Image.merge 抛
    "ValueError: wrong number of bands"，把整次保存带崩。
    """
    if value is None or isinstance(value, (str, bytes)):
        return tuple(default)
    try:
        gains = tuple(float(g) for g in value)
    except (TypeError, ValueError):
        return tuple(default)
    if len(gains) != 3:
        return tuple(default)
    if not all(math.isfinite(g) for g in gains):
        return tuple(default)
    return gains


def _auto_enhance(img: Image.Image) -> Image.Image:
    """
    文档扫描自动增强 — 高斯模糊除法（CamScanner / ScanTailor 同源算法）。
    原理：降采样估算背景光照 → 原图 ÷ 背景 → 灰底变纯白。
    用 PIL ImageMath 做 C 级像素运算，全分辨率 < 0.5 秒。
    """
    from PIL import ImageFilter, ImageMath

    is_rgb = img.mode == "RGB"
    w, h = img.size

    # 降采样到 1/4 做模糊（只估算背景，不需要高分辨率）
    ds = 4
    sw, sh = max(1, w // ds), max(1, h // ds)

    channels = img.split() if is_rgb else (img,)
    result_channels = []

    for ch in channels:
        # 降采样 → 模糊 → 升采样 = 背景光照估计
        small = ch.resize((sw, sh), Image.BILINEAR)
        blur_r = max(10, min(sw, sh) // 8)
        small_blur = small.filter(ImageFilter.GaussianBlur(radius=blur_r))
        bg = small_blur.resize((w, h), Image.BILINEAR)

        # C 级像素除法：orig * 255 / bg
        # 转 "L" 模式自动裁剪到 [0, 255]
        # +1.0 防止 bg=0 时除零（纯黑像素保护）
        ch_f = ch.convert("F")
        bg_f = bg.convert("F")
        divided = ImageMath.lambda_eval(
            lambda args: args["a"] * 255.0 / (args["b"] + 1.0),
            a=ch_f, b=bg_f
        )
        result_channels.append(divided.convert("L"))

    if is_rgb:
        return Image.merge("RGB", tuple(result_channels))
    return result_channels[0]


def _gamma_lut(gamma: float) -> list:
    """生成 gamma 校正 LUT。gamma > 1 变亮，< 1 变暗。"""
    # 非法 gamma（0 / 负数 / 非数字）⇒ 回落 1.0 —— 否则 1.0/gamma 会除零、
    # 0.0 ** 负数会抛 ZeroDivisionError。
    gamma = sanitize_number(gamma, DEFAULT_GAMMA, GAMMA_MIN, GAMMA_MAX, "gamma")
    inv = 1.0 / gamma
    return [max(0, min(255, int(255 * ((v / 255) ** inv)))) for v in range(256)]


def _shadow_highlight_lut(shadows: int, highlights: int) -> list:
    """
    阴影/高光分离 LUT。
    shadows: -100~100 (0=不变, 正=提亮暗部, 负=压暗暗部)
    highlights: -100~100 (0=不变, 正=提亮亮部, 负=压暗亮部)
    """
    lut = []
    for v in range(256):
        # 阴影影响 0-127 区间，高光影响 128-255 区间
        if v < 128:
            # 阴影调整：用正弦曲线平滑过渡
            t = v / 127.0
            adj = shadows * (1.0 - t) * 0.5
        else:
            # 高光调整
            t = (v - 128) / 127.0
            adj = highlights * t * 0.5
        lut.append(max(0, min(255, int(v + adj))))
    return lut


def apply_exposure(img: Image.Image, mode: str = "off",
                   brightness: int = 0, contrast: int = 0,
                   gamma: float = 1.0, shadows: int = 0, highlights: int = 0,
                   channel_gains: tuple = (1.0, 1.0, 1.0),
                   mime: str = "image/jpeg") -> Image.Image:
    """
    统一曝光后处理入口。
    mode: "off" | "auto" | "manual"
    auto: 高斯模糊除法去灰底
    manual: channel_gains → shadows/highlights → gamma → brightness/contrast
    直接操作 PIL Image 对象，避免 bytes 序列化往返。

    ★ **非法参数回落默认值，不抛异常**（N3）：
      gamma 非数字 / ≤0 / 超出 [0.1, 3.0] ⇒ 1.0；
      通道增益长度 ≠ 3 或含非数字 ⇒ (1.0, 1.0, 1.0)；
      明暗 / 对比度 / 阴影 / 高光 非数字 ⇒ 0（超范围也 ⇒ 0）；
      未知 mode ⇒ "off"（原样返回）。
      理由：预设 JSON 由用户编辑，一个坏数字不该让整批扫描件保存失败。
      **类型完全不对**（dict / list 之类）仍抛 TypeError —— 那是内部契约破坏。
    """
    mode = mode.strip().lower() if isinstance(mode, str) else ""
    if mode not in ("auto", "manual"):
        # "off" 与未知 mode 都原样返回（未知 mode 不该被当成 manual 偷偷改像素）
        return img

    # 数值参数净化：非法 ⇒ 回落默认值（外部脏数据不炸，内部契约破坏仍抛）
    brightness = sanitize_int(brightness, 0, PARAM_MIN, PARAM_MAX, "brightness")
    contrast = sanitize_int(contrast, 0, PARAM_MIN, PARAM_MAX, "contrast")
    shadows = sanitize_int(shadows, 0, PARAM_MIN, PARAM_MAX, "shadows")
    highlights = sanitize_int(highlights, 0, PARAM_MIN, PARAM_MAX, "highlights")
    gamma = sanitize_number(gamma, DEFAULT_GAMMA, GAMMA_MIN, GAMMA_MAX, "gamma")
    channel_gains = sanitize_gains(channel_gains)

    # 确保图像模式为 RGB 或 L（扫描仪可能产出 P/PA 等非标准模式）
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # auto 模式：高斯模糊除法文档增强（去灰底 + 均匀化光照）
    if mode == "auto":
        return _auto_enhance(img)

    # manual 模式：按顺序叠加所有调整
    is_rgb = img.mode == "RGB"

    def _apply_lut(image, lut_256):
        """对 L 模式直接用 256 条目 LUT；对 RGB 重复 3 次凑 768 条目。"""
        if image.mode == "RGB":
            return image.point(lut_256 * 3)
        return image.point(lut_256)

    # Step 1: RGB 通道增益（修复偏色）
    if is_rgb and channel_gains != (1.0, 1.0, 1.0):
        channels = img.split()
        adjusted = []
        for ch, gain in zip(channels, channel_gains):
            if gain != 1.0:
                lut = [max(0, min(255, int(v * gain))) for v in range(256)]
                adjusted.append(ch.point(lut))
            else:
                adjusted.append(ch)
        img = Image.merge("RGB", tuple(adjusted))

    # Step 2: 阴影/高光分离
    if shadows != 0 or highlights != 0:
        lut = _shadow_highlight_lut(shadows, highlights)
        img = _apply_lut(img, lut)

    # Step 3: Gamma 校正
    if gamma != 1.0:
        lut = _gamma_lut(gamma)
        img = _apply_lut(img, lut)

    # Step 4: 亮度/对比度
    if brightness != 0 or contrast != 0:
        factor = (259 * (contrast + 255)) / (255 * (259 - contrast)) if contrast != 0 else 1.0
        lut = [max(0, min(255, int(factor * (v - 128) + 128 + brightness))) for v in range(256)]
        img = _apply_lut(img, lut)

    # Step 5 已移除：手动模式不再无条件做自动拉伸，
    # 避免与「自动」模式效果重复。用户需要拉伸时可切换到自动模式。

    return img


def _img_to_bytes(img: Image.Image, mime: str = "image/jpeg") -> bytes:
    """PIL Image → bytes，按 MIME 选择格式。"""
    buf = io.BytesIO()
    fmt = "JPEG" if mime == "image/jpeg" else "PNG"
    img.save(buf, fmt)
    return buf.getvalue()


def auto_exposure(data: bytes, mime: str = "image/jpeg") -> bytes:
    """自动曝光后处理 — 直方图拉伸。（向后兼容的 bytes 接口）"""
    try:
        img = Image.open(io.BytesIO(data))
        result = apply_exposure(img, mode="auto", mime=mime)
        return _img_to_bytes(result, mime)
    except Exception as e:
        logger.debug("自动曝光处理失败: %s", e)
        return data


def manual_exposure(data: bytes, brightness: int = 0, contrast: int = 0,
                    mime: str = "image/jpeg") -> bytes:
    """手动曝光后处理 — 亮度/对比度调整。（向后兼容的 bytes 接口）"""
    if brightness == 0 and contrast == 0:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        result = apply_exposure(img, mode="manual", brightness=brightness,
                                contrast=contrast, mime=mime)
        return _img_to_bytes(result, mime)
    except Exception as e:
        logger.debug("手动曝光处理失败: %s", e)
        return data
