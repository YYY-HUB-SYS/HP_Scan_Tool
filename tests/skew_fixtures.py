"""
共享的"真实斜页"夹具 —— 批次 4 的纠偏测试用。

★ 为什么不能用 `page.rotate(angle, expand=True, fillcolor=255)`
------------------------------------------------------------------
1. **填充色会变成纯红**：Pillow 对 **RGB** 图像的**标量** `fillcolor` 解释为
   **打包整数** ⇒ 255 = `(255, 0, 0)` 纯红（只有 mode "1"/"L" 下 255 才是白）。
   ⇒ 斜页被一圈**红框**包住。
2. **检测力来自红框而不是页面**：投影法找的是"行投影最锐利"的角度，红框是极强的
   水平/垂直结构 ⇒ 测出来的角度是红框的角度，不是页面内容的角度。
   真机扫描件本身近乎空白（ink ≈ 0.00022，**低于**检测下限 0.005）⇒
   去掉红框后同一张图直接 `skew=None`（这是**正确**行为：空白页本来就测不出角度）。
3. **旧测试是"假通过"**：纠偏时若也用红色填充，红角会**掩盖**残余误差 ⇒
   "纠偏后更直"的断言在红框下恒成立，掩盖了真实精度问题。

⇒ 本模块的做法：**在页面上合成真实的文字行结构**（检测力来自内容），
再用**按模式选的白填充**旋转（不留红框）。

角度符号约定：Pillow 的 `rotate` 是**逆时针为正**（`rotate(+10)` 时顶部标记向左移）。
"""

import random

from PIL import Image, ImageChops, ImageDraw

__all__ = [
    "make_text_page",
    "add_text_rows",
    "rotate_white",
    "white_fill_for",
    "corner_pixels",
    "assert_corners_white",
]


def white_fill_for(img):
    """按图像模式取"白"的正确表示。

    ★ Pillow 的 `rotate(fillcolor=...)`：
      - mode "1" / "L"  → 标量即灰度值，255 = 白
      - mode "RGB"       → 标量被当成**打包整数**，255 = (255,0,0) **纯红**
      - mode "RGBA"      → 同上，(255,0,0,255) 纯红
      - mode "P"         → 标量是**调色板索引**，不是颜色
    """
    mode = getattr(img, "mode", "RGB")
    if mode in ("1", "L"):
        return 255
    if mode == "RGBA":
        return (255, 255, 255, 255)
    return (255, 255, 255)


def make_text_page(width=1200, height=1700, seed=7):
    """白底 + 随机长度的深色横条，模拟文本行（灰度）。"""
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


def add_text_rows(img, seed=7):
    """在**任意模式**的图上叠一层文字行结构（检测力来自内容，不是来自边框）。

    返回与入参同模式的图。灰度页直接加深；彩色页先转灰度加深再转回原模式。
    """
    if img.mode == "P":
        img = img.convert("RGB")
    if img.mode == "RGBA":
        text = make_text_page(img.width, img.height, seed).convert("RGBA")
        text.putalpha(Image.new("L", img.size, 255))
    elif img.mode == "RGB":
        text = make_text_page(img.width, img.height, seed).convert("RGB")
    else:
        text = make_text_page(img.width, img.height, seed)
    return ImageChops.darker(img, text)


def rotate_white(img, angle, resample=Image.BICUBIC, expand=True):
    """按**白**填充旋转（不产生红框）。Pillow 逆时针为正。"""
    if angle == 0:
        return img
    return img.rotate(angle, resample=resample, expand=expand,
                      fillcolor=white_fill_for(img))


def corner_pixels(img):
    """取四角像素（用于断言"纠偏后四角是白"）。"""
    w, h = img.size
    return [img.getpixel(p) for p in
            ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]


def assert_corners_white(img, tol=2):
    """断言四角接近白色。

    回归意义：Pillow 对 RGB/RGBA 的标量 fillcolor 是**打包整数**，
    255 会变成纯红 ⇒ 一旦有人改回标量 255，这条立刻红。
    """
    corners = corner_pixels(img)
    for c in corners:
        if isinstance(c, (int, float)):
            assert abs(c - 255) <= tol, f"四角应为白，实际 {c}"
        else:
            rgb = tuple(c[:3])
            assert all(abs(v - 255) <= tol for v in rgb), \
                f"四角应为白，实际 {corners}"
    return corners
