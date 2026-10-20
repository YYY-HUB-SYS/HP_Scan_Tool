"""
skew_detect 单元测试 — HP Scan Tool v4.0

覆盖:
  * 符号约定（PIL rotate(+3) -> estimate_skew 返回 -3.0）——本模块最易错处
  * 各角度估计误差
  * 纠偏闭环（纠偏后再次估计应回到 0 附近）
  * 空白/低墨迹保护、纯色图、噪声图等退化输入
  * 零新依赖约束（不许出现 numpy/scipy/skimage/deskew）
"""

import ast
import random
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import skew_detect
from skew_detect import deskew_image, estimate_skew, ink_ratio


# ---------------------------------------------------------------- fixtures

def make_text_page(width=1200, height=1700, seed=7):
    """白底 + 随机长度的深色横条，模拟文本行。"""
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    y = 60
    while y < height - 60:
        x0 = rnd.randint(60, 120)
        x1 = rnd.randint(width - 300, width - 60)
        if x1 <= x0:
            x1 = x0 + 80
        draw.rectangle([x0, y, x1, y + rnd.randint(8, 16)],
                       fill=rnd.randint(0, 90))
        y += rnd.randint(28, 42)
    return img


def rotated_page(angle, **kwargs):
    """把合成页用 PIL 旋转 angle 度。

    注意：Pillow 的 rotate 是**逆时针为正**（实测：顶部标记 rotate(+10)
    向左移动）。本项目一律用 PIL 的角度语义。
    """
    page = make_text_page(**kwargs)
    if angle == 0:
        return page
    return page.rotate(angle, resample=Image.BICUBIC, fillcolor=255)


def near_blank_page(width=1200, height=1700, seed=3):
    """近空白页：白底 + 稀疏浅色噪点（模拟真机空白玻璃）。"""
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for _ in range(600):
        x = rnd.randrange(width)
        y = rnd.randrange(height)
        draw.point((x, y), fill=rnd.randint(150, 235))
    return img


def noise_page(width=600, height=800, seed=11):
    """满屏随机灰度噪声。"""
    rnd = random.Random(seed)
    img = Image.new("L", (width, height))
    img.putdata([rnd.randrange(256) for _ in range(width * height)])
    return img


@pytest.fixture(scope="module")
def text_page():
    return make_text_page()


# ---------------------------------------------------------------- 符号约定

class TestSignConvention:
    """★ 符号约定：PIL rotate(theta) 生成的图 -> estimate_skew 返回 -theta。"""

    def test_positive_three_degrees_returns_minus_three(self):
        page = rotated_page(3.0)
        assert estimate_skew(page) == pytest.approx(-3.0, abs=0.5)

    def test_negative_three_degrees_returns_plus_three(self):
        page = rotated_page(-3.0)
        assert estimate_skew(page) == pytest.approx(3.0, abs=0.5)

    def test_estimate_is_exactly_negated_generating_angle(self):
        """估计量与生成角严格反号（不取负就无法直接施加给 PIL）。"""
        for angle in (1.0, 2.0, 3.0, 5.0):
            est = estimate_skew(rotated_page(angle))
            assert est * angle < 0, (angle, est)

    def test_deskew_closes_the_loop(self):
        """纠偏闭环：纠偏后再次估计应回到 0 附近。"""
        for angle in (3.0, -3.0, 5.0):
            page = rotated_page(angle)
            est = estimate_skew(page)
            corrected = deskew_image(page, est)
            assert estimate_skew(corrected) == pytest.approx(0.0, abs=0.5), angle

    def test_deskew_reduces_tilt(self):
        """纠偏后残余倾角应明显小于纠偏前。"""
        page = rotated_page(4.0)
        before = abs(estimate_skew(page))
        after = abs(estimate_skew(deskew_image(page, estimate_skew(page))))
        assert after < before


# ---------------------------------------------------------------- 角度精度

class TestAngleAccuracy:

    def test_upright_page_is_near_zero(self):
        assert estimate_skew(rotated_page(0.0)) == pytest.approx(0.0, abs=0.25)

    @pytest.mark.parametrize("angle", [1.0, -1.0, 2.0, -2.0, 3.0, -3.0, 5.0, -5.0])
    def test_angle_error_within_half_degree(self, angle):
        est = estimate_skew(rotated_page(angle))
        assert est == pytest.approx(-angle, abs=0.5)
        # 比验收线更严：实测误差 0.000 度，不允许贴着 0.5 边界过关
        assert abs(est + angle) < 0.5

    def test_result_within_search_range(self):
        est = estimate_skew(rotated_page(7.0))
        assert -8.0 <= est <= 8.0

    def test_full_resolution_page(self):
        """300dpi A4 尺寸（2480x3507）也要能测出来。"""
        page = make_text_page(2480, 3507, seed=13)
        tilted = page.rotate(3.0, resample=Image.BICUBIC, fillcolor=255)
        assert estimate_skew(tilted) == pytest.approx(-3.0, abs=0.5)


# ---------------------------------------------------------------- 空白页保护

class TestBlankPageGuard:

    def test_near_blank_page_returns_none(self):
        page = near_blank_page()
        assert ink_ratio(page) < 0.005
        assert estimate_skew(page) is None

    def test_pure_white_returns_none(self):
        assert estimate_skew(Image.new("L", (800, 1000), 255)) is None

    def test_pure_black_returns_none(self):
        """纯黑图没有任何行结构 -> 无从判角度。"""
        assert estimate_skew(Image.new("L", (800, 1000), 0)) is None

    def test_text_page_is_not_treated_as_blank(self):
        assert ink_ratio(rotated_page(0.0)) > 0.05
        assert estimate_skew(rotated_page(0.0)) is not None

    def test_min_ink_ratio_parameter_is_honoured(self):
        page = rotated_page(0.0)
        assert estimate_skew(page, min_ink_ratio=0.99) is None
        assert estimate_skew(page, min_ink_ratio=0.0) is not None


# ---------------------------------------------------------------- 退化输入

class TestDegenerateInput:

    def test_noise_page_does_not_crash(self):
        result = estimate_skew(noise_page())
        assert result is None or -8.0 <= result <= 8.0

    def test_none_image_returns_none(self):
        assert estimate_skew(None) is None

    def test_tiny_image_does_not_crash(self):
        result = estimate_skew(Image.new("L", (4, 4), 255))
        assert result is None or isinstance(result, float)


# ---------------------------------------------------------------- deskew_image

class TestDeskewImage:

    def test_none_angle_returns_input_unchanged(self):
        page = rotated_page(3.0)
        assert deskew_image(page, None) is page

    def test_zero_angle_returns_input_unchanged(self):
        page = rotated_page(3.0)
        assert deskew_image(page, 0.0) is page

    def test_rotation_applied_is_pil_counter_clockwise_positive(self):
        """deskew_image 的旋转量直接喂给 PIL rotate（逆时针为正）。"""
        page = make_text_page(400, 600)
        out = deskew_image(page, 2.0)
        expected = page.rotate(2.0, resample=Image.BICUBIC, fillcolor=255)
        assert out.size == page.size
        assert list(out.convert("L").tobytes()) == list(expected.convert("L").tobytes())

    def test_size_preserved(self):
        page = rotated_page(3.0)
        out = deskew_image(page, estimate_skew(page))
        assert out.size == page.size

    # ── 回归 N1：模式白名单不全 ⇒ LA/F/I/I;16 直接抛 TypeError ──────────
    #
    # 缺陷背景：修 P1（RGB 图的**标量** fillcolor=255 被 Pillow 解释成打包整数
    # ⇒ 四角纯红，实测占页面 4.36%）时，填充色改成了「按模式取白」，
    # 但那份**白名单只覆盖了 1 / L / RGBA / RGB** ⇒ `LA` / `F` / `I` / `I;16`
    # 这些模式拿到三元组白 ⇒ Pillow 直接抛 TypeError
    # ⇒ 那一页**整页落盘失败**（save_manager 捕获后计入 failed_pages，
    #   用户根本拿不到这一页）。
    # 正确做法：按**波段数**生成白元组（bands==1 ⇒ 标量 255，否则 n 元组）。

    #: 各模式下的「白」初值（CMYK 的 0 才是白，不参与角像素断言）
    _WHITE_INIT = {
        "1": 255,
        "L": 255,
        "LA": (255, 255),
        "I": 255,
        "I;16": 65535,
        "F": 255.0,
        "RGB": (255, 255, 255),
        "RGBA": (255, 255, 255, 255),
    }

    @pytest.mark.parametrize("mode", ["1", "L", "LA", "I", "I;16", "F",
                                       "RGB", "RGBA", "CMYK", "P"])
    def test_every_pillow_mode_is_supported(self, mode):
        """★ 回归 N1：任何 Pillow 模式纠偏都**不能抛 TypeError**。

        现实触发概率低（扫描档位只有 RGB24/Grayscale8 ⇒ RGB/L），
        但 `_read_cache_image` 是 `Image.open(...).copy()`，**原样保留源模式**
        ⇒ 只要扫描仪吐出 16 位 PNG / 32 位 TIFF 就会命中 ⇒ 该页整页丢失。
        """
        img = Image.new(mode, (40, 40))
        out = deskew_image(img, 3.0)
        assert out is not None, mode
        assert out.size == img.size, mode
        assert out.mode in (mode, "RGB"), (mode, out.mode)

    @pytest.mark.parametrize("mode", ["1", "L", "LA", "I", "I;16", "F",
                                       "RGB", "RGBA"])
    def test_fill_is_white_in_every_supported_mode(self, mode):
        """填充色必须是**白**，不是 RGB 标量那种"打包整数 = 纯红"。"""
        img = Image.new(mode, (40, 40), self._WHITE_INIT[mode])
        out = deskew_image(img, 3.0)
        corner = out.convert("RGB").getpixel((0, 0))
        assert all(abs(v - 255) <= 8 for v in corner), (mode, corner)


# ---------------------------------------------------------------- ink_ratio

class TestInkRatio:

    def test_pure_white_is_zero(self):
        assert ink_ratio(Image.new("L", (200, 200), 255)) == 0.0

    def test_pure_black_is_one(self):
        assert ink_ratio(Image.new("L", (200, 200), 0)) == 1.0

    def test_half_black_is_half(self):
        img = Image.new("L", (200, 200), 255)
        ImageDraw.Draw(img).rectangle([0, 0, 99, 199], fill=0)
        assert ink_ratio(img) == pytest.approx(0.5, abs=0.01)

    def test_text_page_ratio_is_high(self):
        assert ink_ratio(make_text_page()) > 0.2


# ---------------------------------------------------------------- 依赖约束

class TestNoExtraDependencies:

    def test_only_pil_and_stdlib_imported(self):
        """本模块不许引入 numpy/scipy/skimage/deskew 之类的新依赖。"""
        source = Path(skew_detect.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imported.add(node.module.split(".")[0])
        assert imported <= {"PIL", "typing"}, imported
