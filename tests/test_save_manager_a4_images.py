"""
save_manager 图片 A4 归一回归 — HP Scan Tool v4.0

契约（用户已拍板：**图片产物与 PDF 产物几何完全一致**）
--------------------------------------------------------
1. 图片落盘路径（jpg / png / tiff）**一律过 `_a4_canvas`**：
   等比缩放 + 居中 + 白边 ⇒ 产物尺寸 = A4 画布（2480×3507@300dpi）。
2. A4 归一是**归一**不是裁切 ⇒ **内容不得丢**（600dpi 源图必须整体缩进来）。
3. 顺序固定：曝光 → 手动旋转 → 自动纠偏 → **A4 归一（永远最后）**。
4. 幂等：已经是 A4 尺寸的图走 `_a4_canvas` 的**提前返回分支** ⇒ 逐字节不变。
5. 图片分支**不得泄漏 PIL 图像对象**（所有权：`canvas is not img` 才 close）。

★ 不跑真机扫描、不写真实用户数据（conftest 已把 APPDATA 重定向到沙箱）。
"""

import gc
import hashlib
import os

import pytest
from PIL import Image

import cache_manager
import history_manager
import save_manager

# A4 @300dpi —— 与 save_manager._a4_canvas 的画布尺寸一致
A4_W, A4_H = 2480, 3507

RED = (220, 30, 30)
BLUE = (30, 30, 220)


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把缓存根目录与历史文件重定向到临时目录（不写真实用户数据）"""
    cache_root = str(tmp_path / "cache")
    os.makedirs(cache_root, exist_ok=True)
    monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: cache_root)
    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: os.path.join(cache_root, "cache_config.json"))
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(cache_manager, "_active_jobs", set())
    monkeypatch.setattr(cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(cache_manager, "_cache_size_cache", None)
    hist = str(tmp_path / "history.json")
    monkeypatch.setattr(history_manager, "_history_file", lambda: hist)
    monkeypatch.setattr(history_manager, "HISTORY_FILE", None)
    return cache_root, hist


# ── 工具 ──────────────────────────────────────────────────────────────


def _solid(path, color, size=(600, 800), fmt="PNG"):
    """造一张纯色页（默认 PNG ⇒ 无损 ⇒ 几何断言可以精确到像素）"""
    img = Image.new("RGB", size, color)
    img.save(path, fmt)
    return str(path)


def _content_bbox(img):
    """非白像素的外接框（None = 整张都是白的）"""
    mask = img.convert("L").point(lambda p: 0 if p > 245 else 255)
    return mask.getbbox()


def _has_ink(img, box):
    """box 区域里有没有墨（用于"四角标记还在不在"）"""
    region = img.convert("L").crop(box)
    return region.getextrema()[0] < 245


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ── A：图片产物必须是 A4 画布 ─────────────────────────────────────────


class TestImageA4Canvas:
    def test_save_single_png_is_a4_normalized_and_not_cropped(self, tmp_path,
                                                                isolated):
        """★ 单页 PNG：产物 = A4 画布，内容**完整且居中**（等比缩放+白边）。"""
        src = _solid(tmp_path / "src.png", RED)
        out = str(tmp_path / "out.png")
        saved = save_manager.save_single(src, "png", out)

        with Image.open(saved) as im:
            im.load()
            assert im.size == (A4_W, A4_H), \
                f"图片产物必须是 A4 画布（PDF 产物也是这个尺寸）：{im.size}"
            bbox = _content_bbox(im)

        assert bbox is not None, "内容整个丢了"
        assert (bbox[2] - bbox[0], bbox[3] - bbox[1]) == (600, 800), \
            f"内容被裁切/缩放了：{bbox}"
        assert bbox[0] == (A4_W - 600) // 2 and bbox[1] == (A4_H - 800) // 2, \
            f"内容没有居中：{bbox}"

    def test_save_single_jpg_is_a4_sized(self, tmp_path, isolated):
        """单页 JPEG：产物同样是 A4 画布（与 PDF 产物一致）。"""
        src = _solid(tmp_path / "src.jpg", RED, fmt="JPEG")
        out = str(tmp_path / "out.jpg")
        saved = save_manager.save_single(src, "jpg", out)
        with Image.open(saved) as im:
            assert im.size == (A4_W, A4_H), f"JPEG 产物不是 A4 画布：{im.size}"

    def test_save_pages_images_are_a4_normalized(self, tmp_path, isolated):
        """★ 多页入口：每一页产物都是 A4 画布。"""
        src = _solid(tmp_path / "src.png", RED)
        base = str(tmp_path / "HP_Scan.png")
        files = save_manager.save_pages("job_a4", "png", base,
                                        [(src, 0), (src, 0)], [[0, 1]])
        assert len(files) == 2
        for path in files:
            with Image.open(path) as im:
                assert im.size == (A4_W, A4_H), f"{path} 不是 A4 画布：{im.size}"

    def test_save_session_images_are_a4_normalized(self, tmp_path, isolated):
        """★ 会话入口：图片产物同样是 A4 画布。"""
        from document_session import new_session

        cache_root, _ = isolated
        src = _solid(tmp_path / "src.png", RED)
        job = "job_sess_a4"
        path = cache_manager.write_page(job, 1, open(src, "rb").read(), "png")

        session = new_session(mode="single", ext="png", dpi=300)
        session.add_pages(job, [path], side="single", page_nos=[1])
        out_base = str(tmp_path / "sess")
        result = save_manager.save_session(session, out_base)
        assert len(result) == 1
        with Image.open(result[0]) as im:
            assert im.size == (A4_W, A4_H), f"会话图片产物不是 A4 画布：{im.size}"

    def test_tiff_is_a4_normalized(self, tmp_path, isolated):
        """TIFF 走同一条归一路径（不能只照顾 jpg/png）。"""
        src = _solid(tmp_path / "src.tiff", RED, fmt="TIFF")
        out = str(tmp_path / "out.tiff")
        saved = save_manager.save_single(src, "tiff", out)
        with Image.open(saved) as im:
            assert im.size == (A4_W, A4_H), f"TIFF 产物不是 A4 画布：{im.size}"

    def test_image_and_pdf_products_have_same_geometry(self, tmp_path, isolated):
        """★ 同一页：图片产物与 PDF 产物的**页面几何一致**（2480×3507）。

        这是本次改动的**核心目标**：同一页的图片与 PDF 尺寸必须一样。
        """
        from pypdf import PdfReader

        src = _solid(tmp_path / "src.png", RED)
        img_out = str(tmp_path / "same.png")
        pdf_out = str(tmp_path / "same.pdf")
        save_manager.save_single(src, "png", img_out)
        save_manager.save_single(src, "pdf", pdf_out)

        with Image.open(img_out) as im:
            img_size = im.size
        box = PdfReader(pdf_out).pages[0].mediabox
        pdf_w = int(round(float(box.width) / 72.0 * 300))
        pdf_h = int(round(float(box.height) / 72.0 * 300))
        assert img_size == (pdf_w, pdf_h), \
            f"图片 {img_size} 与 PDF {pdf_w}x{pdf_h} 的几何不一致"


# ── B：不裁切 ─────────────────────────────────────────────────────────


class TestNoCropping:
    def test_600dpi_source_is_scaled_not_cropped(self, tmp_path, isolated):
        """★ 600dpi 源图（4960×7015）**整体缩进** A4 画布，四角标记都还在。

        旧实现直接 paste ⇒ 负偏移 ⇒ PIL **裁切** ⇒ 75% 内容被切掉。
        """
        img = Image.new("RGB", (4960, 7015), "white")
        for box in ((0, 0, 200, 200),
                    (4760, 0, 4960, 200),
                    (0, 6815, 200, 7015),
                    (4760, 6815, 4960, 7015)):
            img.paste((0, 0, 0), box)
        src = tmp_path / "big.png"
        img.save(src, "PNG")

        out = str(tmp_path / "big_out.png")
        saved = save_manager.save_single(str(src), "png", out)
        with Image.open(saved) as im:
            im.load()
            assert im.size == (A4_W, A4_H)
            corners = [
                (0, 0, 120, 120),
                (A4_W - 120, 0, A4_W, 120),
                (0, A4_H - 120, 120, A4_H),
                (A4_W - 120, A4_H - 120, A4_W, A4_H),
            ]
            for box in corners:
                assert _has_ink(im, box), \
                    f"600dpi 源图被裁切了：角 {box} 的内容不见了"


# ── C：顺序（归一永远在最后）─────────────────────────────────────────


class TestOrder:
    def test_a4_normalization_happens_after_manual_rotation(self, tmp_path,
                                                            isolated):
        """★ 顺序：手动旋转 → A4 归一。

        源图 600×800（竖）⇒ 旋转 90° 后是 800×600（横）⇒
        画布里的内容区必须仍是"横"的 ⇒ 证明归一在旋转**之后**。
        """
        src = _solid(tmp_path / "src.png", BLUE)
        out = str(tmp_path / "rot.png")
        saved = save_manager.save_single(src, "png", out, rotation=90)
        with Image.open(saved) as im:
            im.load()
            assert im.size == (A4_W, A4_H)
            bbox = _content_bbox(im)
        assert bbox is not None
        assert (bbox[2] - bbox[0], bbox[3] - bbox[1]) == (800, 600), \
            f"旋转没在归一之前生效：{bbox}"


# ── D：幂等 ───────────────────────────────────────────────────────────


class TestIdempotency:
    def test_a4_sized_jpeg_output_is_byte_identical(self, tmp_path, isolated):
        """★ 幂等：已经是 A4 尺寸的 JPEG ⇒ 产物与源**逐字节相同**。

        `_a4_canvas` 对尺寸精确匹配的图**提前返回**（像素一个都不动）⇒
        归一是幂等的，产物字节不变。
        """
        img = Image.new("RGB", (A4_W, A4_H), (12, 34, 56))
        src = tmp_path / "a4.jpg"
        img.save(src, "JPEG", quality=95)

        base = str(tmp_path / "HP_Scan.jpg")
        result = save_manager.save_pages("job_idem", "jpg", base,
                                          [(str(src), 0)], [[0]])
        assert len(result) == 1
        assert _sha(result[0]) == _sha(str(src)), \
            "已经是 A4 尺寸的源图，归一后字节必须不变"

    def test_a4_sized_png_pixels_are_unchanged(self, tmp_path, isolated):
        """单页入口同理：已经是 A4 尺寸的 PNG ⇒ 像素一个都不动。"""
        img = Image.new("RGB", (A4_W, A4_H), (12, 34, 56))
        img.putpixel((10, 10), (255, 0, 0))
        src = tmp_path / "a4.png"
        img.save(src, "PNG")

        out = str(tmp_path / "single.png")
        saved = save_manager.save_single(str(src), "png", out)
        with Image.open(saved) as im:
            im.load()
            assert im.size == (A4_W, A4_H)
            assert im.convert("RGB").getpixel((10, 10)) == (255, 0, 0)


# ── E：不得泄漏 PIL 图像对象 ─────────────────────────────────────────


class TestNoImageLeak:
    def test_image_path_does_not_leak_pil_images(self, tmp_path, isolated):
        """★ 所有权：图片分支不得把 PIL 图像对象泄漏出去。

        既有代码专门修过"PIL 惰性插件 + GC 崩溃"的问题 ⇒ 归一多出来的
        画布必须被 close（`canvas is not img` 时）。
        """
        src = _solid(tmp_path / "src.png", RED)
        gc.collect()
        before = sum(1 for o in gc.get_objects() if isinstance(o, Image.Image))
        base = str(tmp_path / "HP_Scan.png")
        save_manager.save_pages("job_leak", "png", base,
                                [(src, 0), (src, 0), (src, 0)], [[0, 1, 2]])
        gc.collect()
        after = sum(1 for o in gc.get_objects() if isinstance(o, Image.Image))
        assert after <= before + 2, \
            f"图片路径泄漏了 PIL 图像对象：{before} → {after}"
