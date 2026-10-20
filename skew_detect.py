"""
倾斜角检测引擎 — HP Scan Tool v4.0

纯 PIL 实现的投影法（projection profile），**零第三方依赖**。
选型依据见 `docs/design/2026-10-20_纠偏实现选型_实测_v1.0.md`：
`deskew` 库要拖进 183.7 MB 依赖链（EXE 62 MB -> 245 MB），而其内部
本质也是投影法，自实现的纯 PIL 版本角度输出与之逐个一致、还快 3-13 倍。

符号约定（★ 本模块最容易搞错的地方，动代码前先读这段）
--------------------------------------------------------------
本模块的一切旋转都用 **PIL 的 `Image.rotate(angle)`**。

**Pillow 的 `rotate` 是逆时针为正**（实测：图顶中央的方块，
`rotate(+10)` 向左移到 x=86、`rotate(-10)` 向右移到 x=114）。
不要按"顺时针为正"理解，那样会整个算反。

设页面被 PIL 旋转了 θ 得到 P（`P = 原始.rotate(θ)`）：

* 把 P 再转 `-θ` 才摆正；
* 行投影锐利度（相邻行投影之差的绝对值之和）在**文本行被拉平**时最大，
  也就是在 `a = -θ` 时最大 —— 即 **锐利度最大处的角度 = 生成角的相反数**；
* 所以 `estimate_skew` **直接返回这个 argmax 本身**，它已经是可直接施加给
  `Image.rotate` 的纠偏量：

      img.rotate(estimate_skew(img))    # 直接摆正，调用方不要再取负

* 反过来讲：真实扫描件若在物理上顺时针歪了 θ 度，`estimate_skew` 会返回
  **+θ**（PIL 角度意义下），施加后正好摆正。

自检锚点（`tests/test_skew_detect.py` 已锁死）：
`PIL rotate(+3)` 生成的图 → `estimate_skew` 返回 `-3.0`。
"""

from typing import Optional

from PIL import Image

__all__ = ["ink_ratio", "estimate_skew", "deskew_image"]


# mode "1" 是位打包的，每个字节的 1 比特个数 = popcount，用它把
# 位数据还原成像素计数（C 层 translate，比逐像素 Python 循环快两个数量级）
_POPCOUNT = bytes(bin(i).count("1") for i in range(256))

# 直角页保护阈值：0 度与最优角锐利度相差不到 0.5% 时优先取 0 度。
# 锐利度曲线在峰值附近很平（实测相邻 0.25 度只差 0.1%），没有这条保护，
# 摆正的页面会被噪声推到 ±0.5 度，白白转一次。
_ZERO_EPS = 5e-3

# 最小有效角度：|angle| 小于该阈值 ⇒ 没有值得纠正的倾斜。
# 取粗搜步长（2.0°）的一半：低于 0.75° 的倾斜肉眼几乎不可见，
# 不值得为它做一次 BICUBIC 重采样（近空白页会被"白转"一次，四角补白）。
_MIN_EFFECTIVE_ANGLE = 0.75

# 稀疏页的墨迹上限：墨迹占比低于此值 ⇒ 页面几乎没有成形的行结构，
# 此时亚度级的"角度"是噪声的产物（实测墨迹 0.66% 的近空白页会给出 ±0.5°
# 的伪角），必须判为"无法判定"（返回 None，不纠偏也不提示）。
#
# ★ 这个阈值**只**用于亚度级判定，不会把正常文档判成空白：
#   真实文档的墨迹占比可能只有百分之几，那种文档只要有真实倾斜
#   （|angle| ≥ 0.75°）照样能正确判角。
#   空白页保护仍由 min_ink_ratio（0.005）负责，本阈值不抬高它。
_MIN_EFFECTIVE_ANGLE_MAX_INK = 0.01


def _to_gray(img):
    """转灰度。已是 L 模式的直接返回，不做无谓的全图拷贝。"""
    return img if img.mode == "L" else img.convert("L")


def _binarize(gray, threshold):
    """灰度 + 二值化（入参须为 L 模式），返回 mode "1"。

    **墨迹(暗) -> 0，纸白(亮) -> 255**

    这个映射方向是必须的，不能反：搜索时旋转二值图用 `fillcolor=255`
    填四角，255 必须代表"纸白"。若把墨迹编码成 255，旋转出来的四个角
    会变成大片假墨迹，行投影被角形楔子带偏（实测 0 度页的估计会偏 0.5 度）。
    """
    return gray.point(lambda p: 0 if p < threshold else 255, mode="1")


def _binary_stats(gray, threshold):
    """二值化并统计，返回 (二值图, 墨迹像素数, 纸白像素数)。入参须为 L 模式。"""
    binary = _binarize(gray, threshold)
    width, height = binary.size
    total = width * height
    if total == 0:
        return binary, 0, 0
    # tobytes() 每行补齐到字节边界，补位恒为 0（算作"墨迹"也不影响总数）
    white = sum(binary.tobytes().translate(_POPCOUNT))
    return binary, total - white, white


def _row_profile(bin_img):
    """行投影：每一行的纸白像素数。

    用纸白而不是墨迹计数：|白[i+1] - 白[i]| 与 |墨[i+1] - 墨[i]| 完全相等
    （白 + 墨 = 行宽），但白像素在 mode "1" 里是"置位"，可以直接 popcount。
    """
    width, height = bin_img.size
    stride = (width + 7) // 8
    data = bin_img.tobytes().translate(_POPCOUNT)
    return [sum(data[y * stride:(y + 1) * stride]) for y in range(height)]


def _sharpness(profile):
    """锐利度 = 相邻行投影之差的绝对值之和。文本行被拉平时最大。"""
    total = 0
    previous = None
    for value in profile:
        if previous is not None:
            total += abs(value - previous)
        previous = value
    return total


def ink_ratio(img, threshold=200) -> float:
    """墨迹占比 = 暗像素数 / 总像素数。用于判定"是不是空白/无文字页"。

    `threshold` 为灰度阈值，灰度 < threshold 的像素算墨迹。
    纯白页 -> 0.0；纯黑页 -> 1.0。
    """
    if img is None:
        return 0.0
    binary, ink, _white = _binary_stats(_to_gray(img), threshold)
    total = binary.width * binary.height
    if total == 0:
        return 0.0
    return ink / total


def _candidate_angles(low, high, step):
    """等距候选角列表（含两端）。"""
    if step <= 0:
        return [low]
    count = int(round((high - low) / step))
    return [round(low + i * step, 6) for i in range(count + 1)]


def _evaluate(binary, angles, best_angle=None, best_score=None):
    """在候选角里找行投影锐利度最大的那个。

    返回 (角度, 锐利度)。锐利度严格更高者胜（峰值附近虽然很平，但真峰值的
    判定必须靠分数本身，不能用"角度小者优先"去偏置）；只在 **0 度**与最优角
    几乎打平时才保 0 度。
    """
    scores = {}
    if best_angle is not None:
        scores[best_angle] = best_score
    for angle in angles:
        rotated = binary.rotate(angle, resample=Image.BILINEAR, fillcolor=255)
        scores[angle] = _sharpness(_row_profile(rotated))
    best_angle = max(scores, key=lambda a: scores[a])
    best_score = scores[best_angle]
    if (0.0 in scores and abs(0.0) < abs(best_angle)
            and scores[0.0] >= best_score * (1.0 - _ZERO_EPS)):
        return 0.0, scores[0.0]
    return best_angle, best_score


def estimate_skew(img, max_angle=8.0, coarse=2.0, fine=0.5,
                  target_width=1000, threshold=200,
                  min_ink_ratio=0.005) -> Optional[float]:
    """估计需要施加给 PIL `Image.rotate` 的旋转角（度）以把文档摆正。

    返回 None 表示"无法判定"（空白页 / 墨迹过少 / 找不到主方向），
    调用方据此**跳过纠偏、也不提示用户**。

    ★ 符号约定（最容易搞错的地方）：
      本项目的旋转一律用 PIL 的 `Image.rotate(angle)`，**Pillow 逆时针为正**
      （实测：顶部标记 `rotate(+10)` 向左移）。
      投影锐利度最大处 = 真实旋转角的**相反数**，所以本函数返回的是
      **已经取过相反数、可直接施加给 PIL 的纠偏量**。
      调用方拿返回值直接 `img.rotate(angle)` 即可摆正，不要再取负。

    参数：
        max_angle     搜索范围 ±max_angle 度
        coarse        粗搜步长（度）
        fine          细搜步长（度）
        target_width  缩略图目标宽度（在此宽度上算角度，不碰全分辨率）
        threshold     灰度二值化阈值，< threshold 算墨迹
        min_ink_ratio 低于此墨迹占比视为空白页，直接返回 None

    ★ 最小有效角度：|angle| < 0.75°（粗搜步长的一半）视为"没有可纠正的倾斜"。
      稀疏页（墨迹占比 < 1%）返回 None（伪角来自噪声）；有结构的页面返回 0.0。
      两者都不纠偏、不提示。
    """
    if img is None:
        return None

    # 1) 低墨迹保护：空白页/近乎空白页不判角度（否则空白页会被白转）
    gray = _to_gray(img)
    binary, ink, white = _binary_stats(gray, threshold)
    total = binary.width * binary.height
    if total == 0:
        return None
    ink_ratio_value = ink / float(total)
    if ink_ratio_value < min_ink_ratio:
        return None
    if white == 0:
        # 整页全黑：一行纸白都没有 => 没有任何行结构，无从判断角度
        return None

    # 2) 缩到 target_width 以内再算角度（角度与分辨率无关，全尺寸太慢）
    work = gray
    if target_width > 0 and work.width > target_width:
        scale = target_width / float(work.width)
        work = work.resize((target_width, max(1, int(work.height * scale))),
                            Image.LANCZOS)
    binary = _binarize(work, threshold)
    if binary.width == 0 or binary.height == 0:
        return None

    # 3) 粗搜：±max_angle，步长 coarse
    coarse_angles = _candidate_angles(-max_angle, max_angle, coarse)
    best_angle, best_score = _evaluate(binary, coarse_angles)
    if best_angle is None:
        return None

    # 4) 细搜：粗搜最优 ±coarse 内，步长 fine
    fine_angles = _candidate_angles(
        max(-max_angle, best_angle - coarse),
        min(max_angle, best_angle + coarse),
        fine,
    )
    best_angle, best_score = _evaluate(
        binary, fine_angles, best_angle, best_score)

    # 5) 没有任何行结构（纯色图等）=> 无从判断
    if not best_score:
        return None

    # 6) 最小有效角度：|angle| < 0.75°（粗搜步长的一半）⇒ 没有值得纠正的倾斜
    #    - 稀疏页（墨迹占比很低）：亚度级角度来自噪声而不是真实倾斜，
    #      判为"无法判定"⇒ 返回 None（不纠偏、不提示），
    #      避免近空白页被白转一次（BICUBIC 重采样 + 四角补白）。
    #    - 有结构的页面：亚度级角度直接归零（0 度 = 摆正），
    #      同样不纠偏、不提示，但保留"这是一个有效判定"的语义。
    if abs(best_angle) < _MIN_EFFECTIVE_ANGLE:
        if ink_ratio_value < _MIN_EFFECTIVE_ANGLE_MAX_INK:
            return None
        return 0.0
    return best_angle


def deskew_image(img, angle, fillcolor=None):
    """按 `estimate_skew` 返回的纠偏量旋转图像（Pillow `rotate` 逆时针为正）。

    `angle` 为 None（或 0）时原样返回；不改变画布尺寸。

    ★ `fillcolor` 缺省时按**图像模式**自动取白：
        - mode "1" / "L"  → 255（灰度白）
        - mode "RGB"      → (255, 255, 255)
        - mode "RGBA"     → (255, 255, 255, 255)
        - mode "P"        → 先转 RGB 再旋转（调色板索引不是颜色）

    ★★ 为什么要按模式选：Pillow 对 **RGB/RGBA** 的**标量** `fillcolor`
    解释为**打包整数** —— 255 会变成 `(255, 0, 0)` **纯红**（不是白！），
    只有 1/L 模式下 255 才是白。
    ⇒ 曾经 RGB 页被自动纠偏后**四角是纯红色块**（实测 5° 斜角时占页面 4.36%）。
    """
    if img is None or angle is None:
        return img
    angle = float(angle)
    if abs(angle) < 1e-9:
        return img
    mode = getattr(img, "mode", "RGB")
    if mode == "P":
        # 调色板图：标量 fillcolor 是**调色板索引**，不是颜色 ⇒ 先转 RGB
        img = img.convert("RGB")
    if fillcolor is None:
        if mode in ("1", "L"):
            fillcolor = 255
        elif mode == "RGBA":
            fillcolor = (255, 255, 255, 255)
        else:
            fillcolor = (255, 255, 255)
    return img.rotate(angle, resample=Image.BICUBIC, fillcolor=fillcolor)
