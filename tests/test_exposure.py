"""
exposure 单元测试 — HP Scan Tool v4.x
覆盖: off 原样返回 / manual 各参数生效 / auto 去灰底 / 模式转换 / bytes 接口 / 300dpi 性能
"""

import io
import time

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

import exposure


# ── 工具 ──────────────────────────────────────────────────────────────


def _synth_document(w=800, h=600, bg=200, fg=40):
    """合成一张"灰底 + 深色文字块"的文档图（模拟扫描件）"""
    img = Image.new("RGB", (w, h), (bg, bg, bg))
    d = ImageDraw.Draw(img)
    for i in range(12):
        y = 40 + i * 45
        d.rectangle([60, y, 60 + 500, y + 18], fill=(fg, fg, fg))
    return img


def _mean(img):
    return ImageStat.Stat(img).mean[0]


def _stddev(img):
    return ImageStat.Stat(img).stddev[0]


def _changed(a, b):
    """两张图是否存在像素差异"""
    return ImageChops.difference(a, b).getbbox() is not None


# ── off 模式 ──────────────────────────────────────────────────────────


class TestOffMode:
    def test_off_returns_pixels_unchanged(self):
        """mode="off" 必须原样返回，像素一个都不动"""
        img = _synth_document()
        out = exposure.apply_exposure(img, mode="off")
        assert out.mode == img.mode
        assert out.size == img.size
        assert out.tobytes() == img.tobytes()

    def test_off_ignores_parameters(self):
        """off 是早退分支：即使传了参数也不该改像素"""
        img = _synth_document()
        out = exposure.apply_exposure(
            img, mode="off", brightness=50, contrast=50,
            gamma=2.0, shadows=50, highlights=50, channel_gains=(2.0, 0.5, 1.0),
        )
        assert out.tobytes() == img.tobytes()


# ── manual 模式 ───────────────────────────────────────────────────────


class TestManualMode:
    def test_brightness_changes_pixels(self):
        """mode="manual" 且 brightness 变化 ⇒ 像素确实变化"""
        img = _synth_document()
        out = exposure.apply_exposure(img, mode="manual", brightness=30)
        assert _changed(img, out)
        assert _mean(out) > _mean(img)

    def test_manual_neutral_params_are_identity(self):
        """全默认参数 ⇒ 与原图一致"""
        img = _synth_document()
        out = exposure.apply_exposure(img, mode="manual")
        assert out.tobytes() == img.tobytes()

    @pytest.mark.parametrize("kwargs", [
        {"gamma": 1.5},
        {"gamma": 0.6},
        {"shadows": 40},
        {"shadows": -40},
        {"highlights": 40},
        {"highlights": -40},
        {"channel_gains": (1.3, 0.9, 1.0)},
    ])
    def test_each_parameter_changes_result(self, kwargs):
        """gamma / shadows / highlights / channel_gains 各自都会改变结果"""
        img = _synth_document()
        base = exposure.apply_exposure(img, mode="manual")
        out = exposure.apply_exposure(img, mode="manual", **kwargs)
        assert _changed(base, out)

    def test_contrast_changes_pixels(self):
        img = _synth_document()
        out = exposure.apply_exposure(img, mode="manual", contrast=60)
        assert _changed(img, out)


# ── auto 模式 ─────────────────────────────────────────────────────────


class TestAutoMode:
    def test_auto_brighter_and_higher_contrast(self):
        """auto 对"带灰底"的合成图 ⇒ 比原图更亮、对比更高"""
        img = _synth_document()
        before_mean, before_std = _mean(img), _stddev(img)
        out = exposure.apply_exposure(img, mode="auto")
        assert _stddev(out) > before_std, "自动增强后标准差未提高"
        assert _mean(out) > before_mean, "自动增强后平均亮度未提高"

    def test_auto_whiteens_background(self):
        """灰底应被拉向纯白"""
        img = _synth_document(bg=200, fg=40)
        out = exposure.apply_exposure(img, mode="auto")
        # 取右下角一块"背景"区域（原值 200）
        bg_patch = out.crop((700, 520, 780, 580)).convert("L")
        assert max(bg_patch.tobytes()) > 200, "灰底没有被提亮"

    def test_auto_on_grayscale_keeps_l(self):
        img = _synth_document().convert("L")
        out = exposure.apply_exposure(img, mode="auto")
        assert out.mode == "L"


# ── 模式转换 ──────────────────────────────────────────────────────────


class TestModeConversion:
    @pytest.mark.parametrize("src_mode,expected", [
        ("P", "RGB"),
        ("LA", "RGB"),
    ])
    def test_non_rgb_input_converted_to_rgb(self, src_mode, expected):
        """P / LA 等非标准模式输入会被正确转成 RGB"""
        img = _synth_document().convert(src_mode)
        assert img.mode == src_mode
        out = exposure.apply_exposure(img, mode="manual", brightness=20)
        assert out.mode == expected
        assert out.size == img.size

    def test_grayscale_input_stays_l(self):
        """L 模式输入保持 L，不做无谓转换"""
        img = _synth_document().convert("L")
        out = exposure.apply_exposure(img, mode="manual", brightness=20)
        assert out.mode == "L"


# ── bytes 兼容接口 ────────────────────────────────────────────────────


class TestBytesInterface:
    def test_auto_exposure_jpeg_roundtrip(self):
        """bytes 接口：JPEG 输入 → JPEG 输出，且被提亮"""
        img = _synth_document()
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        data = buf.getvalue()
        out = exposure.auto_exposure(data, mime="image/jpeg")
        assert out[:2] == b"\xff\xd8", "JPEG magic 不符"
        result = Image.open(io.BytesIO(out))
        assert _mean(result.convert("RGB")) > _mean(img)

    def test_auto_exposure_png_keeps_png(self):
        """mime=image/png 时输出仍是 PNG"""
        img = _synth_document()
        buf = io.BytesIO()
        img.save(buf, "PNG")
        out = exposure.auto_exposure(buf.getvalue(), mime="image/png")
        assert out[:8] == b"\x89PNG\r\n\x1a\n", "PNG magic 不符"

    def test_manual_exposure_noop_returns_input(self):
        """亮度/对比度都为 0 时原样返回"""
        img = _synth_document()
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        data = buf.getvalue()
        assert exposure.manual_exposure(data, brightness=0, contrast=0) is data

    def test_manual_exposure_changes_bytes(self):
        img = _synth_document()
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        data = buf.getvalue()
        out = exposure.manual_exposure(data, brightness=40, contrast=20)
        assert out != data


# ── 性能 ──────────────────────────────────────────────────────────────


def test_auto_mode_300dpi_timing():
    """300dpi A4（2480×3508）单页 auto 耗时基准。

    ADR 0008 声称约 0.4 秒；这里只设宽松上限（5s）防病态回归，
    实测值由 print 输出（pytest -s 可见）。
    """
    img = _synth_document(2480, 3508)
    start = time.perf_counter()
    exposure.apply_exposure(img, mode="auto")
    elapsed = time.perf_counter() - start
    print(f"\n[PERF] auto 300dpi 2480x3508: {elapsed:.3f} s")
    assert elapsed < 5.0, f"auto 模式耗时 {elapsed:.3f}s，超出宽松上限 5s"
