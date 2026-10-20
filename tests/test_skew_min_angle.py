"""
skew_detect 最小有效角度回归测试 — HP Scan Tool v4.0

锁死 m-1（Minor）：近空白页的伪角。

复核方实测（墨迹占比刚过 0.5% 阈值时）：
    ink_ratio=0.00441 -> None
    ink_ratio=0.00661 -> +0.5      <- 越过阈值即产生伪角
    ink_ratio=0.00688 -> -0.5

±0.5° 的伪角会被 deskew_image 实际旋转（BICUBIC 重采样 + 四角补白），
近空白页被白白转一次。

修法（"最小有效角度"）：|angle| < 0.75°（粗搜步长的一半）视为
"没有可纠正的倾斜"：
  - 稀疏页（墨迹占比 < 1%）⇒ 返回 None（伪角来自噪声，无法判定）
  - 有结构的页面 ⇒ 返回 0.0（同样不纠偏、不提示，但保留"有效判定"语义）

★ min_ink_ratio 保持 0.005 不抬高：真实文档的墨迹占比可能只有百分之几，
  那种文档只要有真实倾斜（|angle| ≥ 0.75°）照样能正确判角。
"""

import inspect
import random

import pytest
from PIL import Image, ImageDraw

import skew_detect
from skew_detect import deskew_image, estimate_skew, ink_ratio


def speckle_page(n=3400, seed=1, size=1, width=1200, height=1700):
    """散点页：白底 + n 个深色小方块（模拟只剩少量噪点的近空白页）"""
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for _ in range(n):
        x = rnd.randrange(width)
        y = rnd.randrange(height)
        draw.rectangle([x, y, x + size, y + size], fill=rnd.randint(0, 120))
    return img


def text_lines_page(n_lines=3, seed=7, width=1200, height=1700):
    """稀疏但**真实**的文档：白底 + n_lines 行文字横条"""
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    y = 60
    i = 0
    while y < height - 60 and i < n_lines:
        x0 = rnd.randint(60, 120)
        x1 = rnd.randint(width - 300, width - 60)
        if x1 <= x0:
            x1 = x0 + 80
        draw.rectangle([x0, y, x1, y + rnd.randint(8, 16)],
                       fill=rnd.randint(0, 90))
        y += rnd.randint(28, 42)
        i += 1
    return img


def _raw_angle(img):
    """关掉"最小有效角度"规则，拿未过滤的原始角度（用于证明伪角确实存在）"""
    old = skew_detect._MIN_EFFECTIVE_ANGLE
    skew_detect._MIN_EFFECTIVE_ANGLE = 0.0
    try:
        return estimate_skew(img)
    finally:
        skew_detect._MIN_EFFECTIVE_ANGLE = old


class TestNearBlankNoPseudoAngle:
    def test_near_blank_page_returns_none(self):
        """墨迹 ~0.66% 的近空白页 ⇒ 返回 None（不再给 ±0.5° 伪角）"""
        page = speckle_page()
        ratio = ink_ratio(page)
        assert 0.006 <= ratio <= 0.0075, f"样本墨迹占比不对: {ratio}"

        assert estimate_skew(page) is None

    def test_raw_angle_really_is_a_pseudo_angle(self):
        """证明上一条不是"本来就判不出"，而是规则把 ±0.5° 伪角过滤掉了"""
        page = speckle_page()
        raw = _raw_angle(page)
        assert raw in (0.5, -0.5), f"原始角度应当是 ±0.5° 伪角，实际 {raw}"
        assert estimate_skew(page) is None

    def test_near_blank_page_is_not_rotated(self):
        """返回 None ⇒ deskew_image 不会被调用去旋转这张近空白页"""
        page = speckle_page()
        angle = estimate_skew(page)
        assert angle is None
        assert deskew_image(page, angle) is page

    def test_min_effective_angle_threshold(self):
        """阈值必须是 0.75°（粗搜步长 2.0° 的一半）"""
        assert skew_detect._MIN_EFFECTIVE_ANGLE == 0.75


class TestRealDocumentsStillWork:
    def test_min_ink_ratio_default_not_raised(self):
        """min_ink_ratio 保持 0.005：不能为了躲开伪角把正常文档判成空白"""
        default = inspect.signature(skew_detect.estimate_skew).parameters[
            "min_ink_ratio"].default
        assert default == 0.005

    def test_sparse_document_with_real_skew_is_measured(self):
        """稀疏（墨迹 ~2%）但**真实倾斜**的文档：照样能正确判角"""
        page = text_lines_page(n_lines=3).rotate(
            2.0, resample=Image.BICUBIC, fillcolor=255)
        ratio = ink_ratio(page)
        assert ratio > 0.01, f"样本墨迹占比不对: {ratio}"

        est = estimate_skew(page)
        assert est is not None, "稀疏但有真实倾斜的文档不能被判成无法判定"
        assert est == pytest.approx(-2.0, abs=0.5)

    def test_sub_degree_skew_on_dense_page_is_zero(self):
        """有结构的页面只有亚度级倾斜 ⇒ 归零（不纠偏、不提示，但不是"无法判定"）"""
        dense = text_lines_page(n_lines=30)
        page = dense.rotate(0.5, resample=Image.BICUBIC, fillcolor=255)

        est = estimate_skew(page)
        assert est is not None
        assert abs(est) < 0.75

    def test_upright_dense_page_is_zero(self):
        page = text_lines_page(n_lines=30)
        est = estimate_skew(page)
        assert est is not None
        assert abs(est) < 0.75

    def test_pure_white_and_black_still_none(self):
        assert estimate_skew(Image.new("L", (800, 1000), 255)) is None
        assert estimate_skew(Image.new("L", (800, 1000), 0)) is None
