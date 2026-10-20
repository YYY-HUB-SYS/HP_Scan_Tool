"""
save_manager 缺陷修复回归 — N1 / N2 / N5
来源：核销报告 .superpowers/sdd/2026-10-20_.../verify-v2a.md §5

  N1  _a4_canvas 返回**已关闭**的图像 ⇒ 灰度/黑白 A4 页在 PDF 里被整页丢弃
  N2  jpg 快路径不校验 ⇒ 损坏字节被复制成 .jpg 且 failed_pages=0
  N5  page_specs=[] 时 save_pages 不报错（返回 ok=False 的空结果）
"""

import hashlib
import os

import pytest
from PIL import Image
from pypdf import PdfReader

import cache_manager
import history_manager
import save_manager

# A4 @300dpi —— 本机平板扫描的真实像素尺寸
A4_W, A4_H = 2480, 3507


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


def _a4_page(path, mode="L", color=200, fmt="JPEG"):
    """造一张 A4@300dpi 的页面（灰度 / 黑白 / 彩色）"""
    img = Image.new(mode, (A4_W, A4_H), color)
    if mode == "1":
        img.paste(0, (100, 100, 900, 900))
    else:
        img.paste(0 if mode == "L" else (0, 0, 0), (100, 100, 900, 900))
    img.save(path, fmt)
    return str(path)


# ────────────── N1：_a4_canvas 不得返回已关闭的图像 ──────────────

class TestA4CanvasOwnership:
    def test_grayscale_a4_canvas_is_usable(self):
        """灰度 A4 页的画布必须**能继续使用**（旧实现返回的是已关闭的图）。

        旧行为：img.mode != "RGB" 且尺寸恰好 = A4 ⇒ 转换出的副本被 finally
        close() 掉 ⇒ 返回值一用就抛 "Operation on closed image"。
        """
        img = Image.new("L", (A4_W, A4_H), 200)
        canvas = save_manager._a4_canvas(img, 300)
        try:
            assert canvas.mode == "RGB"
            assert canvas.size == (A4_W, A4_H)
            # 一用就炸 = 已关闭
            assert canvas.getpixel((0, 0)) == (200, 200, 200)
        finally:
            if canvas is not img:
                canvas.close()

    def test_black_white_a4_canvas_is_usable(self):
        """黑白（mode="1"）同样不能丢。"""
        img = Image.new("1", (A4_W, A4_H), 1)
        canvas = save_manager._a4_canvas(img, 300)
        try:
            assert canvas.getpixel((0, 0)) == (255, 255, 255)
        finally:
            if canvas is not img:
                canvas.close()

    def test_rgb_a4_canvas_still_returns_input_itself(self):
        """RGB + 精确匹配 ⇒ 仍返回入参本身（既有契约不能破）。"""
        img = Image.new("RGB", (A4_W, A4_H), "white")
        canvas = save_manager._a4_canvas(img, 300)
        assert canvas is img

    def test_small_image_still_padded_and_centered(self):
        """小图留白居中的行为不能被修复顺带改掉。"""
        img = Image.new("RGB", (100, 200), "white")
        canvas = save_manager._a4_canvas(img, 300)
        try:
            assert canvas.size == (A4_W, A4_H)
            assert canvas.getpixel((0, 0)) == (255, 255, 255)
            # 图像左上角应落在居中位置
            assert canvas.getpixel(((A4_W - 100) // 2, (A4_H - 200) // 2)) == (255, 255, 255)
        finally:
            if canvas is not img:
                canvas.close()


class TestGrayscaleA4Pdf:
    def test_single_grayscale_a4_page_produces_pdf(self, tmp_path, isolated):
        """单页灰度 A4 ⇒ 必须出 PDF（旧行为直接抛 RuntimeError）。"""
        src = _a4_page(tmp_path / "gray.jpg", mode="L")
        out = str(tmp_path / "gray.pdf")
        saved = save_manager.save_single(src, "pdf", out)
        assert os.path.getsize(saved) > 0
        assert len(PdfReader(saved).pages) == 1

    def test_grayscale_page_not_dropped_from_multipage_pdf(self, tmp_path, isolated):
        """灰度页 + 彩色页 ⇒ PDF 必须是 2 页（旧行为灰度页被静默丢弃）。"""
        gray = _a4_page(tmp_path / "gray.jpg", mode="L")
        rgb = _a4_page(tmp_path / "rgb.jpg", mode="RGB", color=(200, 200, 200))
        out = str(tmp_path / "mix.pdf")
        result = save_manager.save_pages("job_gray", "pdf", out,
                                         [(gray, 0), (rgb, 0)], [[0, 1]])
        assert len(result) == 1
        assert result.failed_pages == 0, "灰度页不应被算作失败"
        assert len(PdfReader(result[0]).pages) == 2, "灰度页被整页丢弃了"

    def test_black_white_page_not_dropped_from_multipage_pdf(self, tmp_path, isolated):
        """黑白（mode="1"）页同样不能丢。"""
        # 用 PNG 承载 mode="1"（JPEG 存不了 1-bit）
        bw = _a4_page(tmp_path / "bw.png", mode="1", fmt="PNG")
        rgb = _a4_page(tmp_path / "rgb.jpg", mode="RGB", color=(200, 200, 200))
        out = str(tmp_path / "bw.pdf")
        result = save_manager.save_pages("job_bw", "pdf", out,
                                         [(bw, 0), (rgb, 0)], [[0, 1]])
        assert len(PdfReader(result[0]).pages) == 2


# ────────────── N2：jpg 快路径必须校验缓存页 ──────────────

class TestJpgFastPathValidation:
    def test_corrupt_cache_page_is_reported_as_failed(self, tmp_path, isolated):
        """损坏的缓存页不能被原样复制成 .jpg 后谎报成功。

        旧行为：22 字节垃圾被复制成 .jpg，failed_pages=0（调用方完全不知情）。
        """
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"not-an-image-at-all-xx")
        out = str(tmp_path / "HP_Scan.jpg")
        with pytest.raises(RuntimeError):
            save_manager.save_pages("job_bad", "jpg", out,
                                    [(str(bad), 0)], [[0]])
        # 一页都没存下来 ⇒ 不允许留下任何产物
        assert not os.path.exists(str(tmp_path / "HP_Scan_001.jpg"))

    def test_one_bad_page_among_good_ones_is_counted(self, tmp_path, isolated):
        """好页 + 坏页 ⇒ 坏页计入 failed_pages / skipped_pages，好页照常落盘。"""
        good = tmp_path / "good.jpg"
        Image.new("RGB", (60, 80), "white").save(good, "JPEG")
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"\x00" * 22)
        out = str(tmp_path / "HP_Scan.jpg")
        result = save_manager.save_pages("job_mix", "jpg", out,
                                         [(str(good), 0), (str(bad), 0)], [[0, 1]])
        assert result.failed_pages == 1
        assert result.skipped_pages == [1]
        assert result.saved_page_count == 1
        assert len(result) == 1
        assert os.path.exists(result[0])
        assert not os.path.exists(str(tmp_path / "HP_Scan_002.jpg"))

    def test_truncated_jpeg_is_not_copied_verbatim(self, tmp_path, isolated):
        """被截断的 JPEG（头还在、尾没了）也不能原样复制出去。

        截断页必须被算作失败页，而不是产出一个打不开的 .jpg。
        """
        good = tmp_path / "good.jpg"
        Image.new("RGB", (400, 500), "white").save(good, "JPEG")
        data = good.read_bytes()
        truncated = tmp_path / "trunc.jpg"
        truncated.write_bytes(data[: len(data) // 2])
        out = str(tmp_path / "HP_Scan.jpg")
        result = save_manager.save_pages("job_trunc", "jpg", out,
                                         [(str(good), 0), (str(truncated), 0)],
                                         [[0, 1]])
        assert result.failed_pages == 1
        assert result.skipped_pages == [1]
        assert len(result) == 1

    def test_fast_path_keeps_bytes_identical(self, tmp_path, isolated):
        """完好且**已经是 A4 尺寸**的 JPEG 仍走快路径 ⇒ 输出与缓存页**逐字节相同**。

        ★ 新契约（图片一律过 `_a4_canvas`）：快路径只对「尺寸恰好 = A4 画布」
          的页成立 —— `_a4_canvas` 对它**提前返回**（像素一个都不动）
          ⇒ 复制原始字节 **==** 归一后的产物（逐字节一致）。
          尺寸不符的页走解码路径（会被缩放/补白并重新编码），
          那条路径由 tests/test_save_manager_a4_images.py 覆盖。
        """
        src = tmp_path / "src.jpg"
        Image.new("RGB", (A4_W, A4_H), (12, 34, 56)).save(src, "JPEG")
        out = str(tmp_path / "HP_Scan.jpg")
        result = save_manager.save_pages("job_fast", "jpg", out,
                                         [(str(src), 0)], [[0]])
        digest_src = hashlib.sha256(src.read_bytes()).hexdigest()
        with open(result[0], "rb") as f:
            digest_out = hashlib.sha256(f.read()).hexdigest()
        assert digest_out == digest_src, "快路径的逐字节一致语义被破坏了"

    # ── 回归 P4：JPEG 快路径绕开 _write_image ⇒ 不建输出目录 ──────────────
    #
    # 缺陷背景：`_write_image` 补了「建父目录」（慢路径 / PDF / save_single
    # 都因此修好），但 JPEG **快路径**是 `shutil.copy2(cache_file, fpath)`
    # —— **绕开** `_write_image` ⇒ 输出目录不存在时 copy2 抛 FileNotFoundError
    # ⇒ **整批页全失败**（正是 P4 的原始症状），而且快路径是最常见的那条。

    def test_fast_path_creates_missing_output_directory(self, tmp_path, isolated):
        """★ 回归 P4：多页 JPEG 走快路径时，输出目录不存在也必须能落盘。"""
        src = tmp_path / "src.jpg"
        Image.new("RGB", (300, 400), (12, 34, 56)).save(src, "JPEG")
        out = str(tmp_path / "not" / "yet" / "HP_Scan.jpg")
        result = save_manager.save_pages("job_mkdir", "jpg", out,
                                         [(str(src), 0)], [[0]])
        assert result.failed_pages == 0, "目录缺失不该让整批页失败"
        assert result.saved_page_count == 1
        assert os.path.exists(result[0]), "产物没落盘"

    def test_save_single_fast_path_creates_missing_output_directory(self, tmp_path,
                                                                     isolated):
        """★ 回归 P4：单页保存走快路径时同样必须先建输出目录。"""
        src = tmp_path / "src.jpg"
        Image.new("RGB", (300, 400), (12, 34, 56)).save(src, "JPEG")
        out = str(tmp_path / "not" / "yet" / "single.jpg")
        path = save_manager.save_single(str(src), "jpg", out, job_id="job_single_mkdir")
        assert os.path.exists(path), "产物没落盘"

    def test_non_jpeg_cache_page_becomes_a_valid_jpeg(self, tmp_path, isolated):
        """PNG 缓存页不再被原样塞进 .jpg（那是个谁也打不开的文件）。"""
        src = tmp_path / "src.png"
        Image.new("RGB", (300, 400), "white").save(src, "PNG")
        out = str(tmp_path / "HP_Scan.jpg")
        result = save_manager.save_pages("job_png", "jpg", out,
                                         [(str(src), 0)], [[0]])
        assert result.failed_pages == 0
        with Image.open(result[0]) as im:
            assert im.format == "JPEG"
            # ★ 新契约：图片也过 A4 归一 ⇒ 产物是 A4 画布（与 PDF 产物一致）
            assert im.size == (A4_W, A4_H), \
                f"图片产物不是 A4 画布：{im.size}"


# ────────────── N5：空页列表必须报错 ──────────────

class TestEmptyPageList:
    def test_empty_page_specs_raises(self, tmp_path, isolated):
        """page_specs=[] + groups=[] ⇒ 必须报错，不能返回"成功但 0 个文件"。"""
        out = str(tmp_path / "HP_Scan.jpg")
        with pytest.raises(RuntimeError):
            save_manager.save_pages("job_empty", "jpg", out, [], [])

    def test_empty_page_specs_with_empty_group_raises(self, tmp_path, isolated):
        """groups=[[]] + page_specs=[] ⇒ 同样报错。"""
        out = str(tmp_path / "HP_Scan.pdf")
        with pytest.raises(RuntimeError):
            save_manager.save_pages("job_empty2", "pdf", out, [], [[]])

    def test_no_files_produced_on_empty_page_list(self, tmp_path, isolated):
        """报错时不得留下任何产物。"""
        out = str(tmp_path / "HP_Scan.jpg")
        with pytest.raises(RuntimeError):
            save_manager.save_pages("job_empty3", "jpg", out, [], [])
        assert not [p for p in os.listdir(str(tmp_path))
                    if p.startswith("HP_Scan")]

    def test_all_pages_failed_still_raises(self, tmp_path, isolated):
        """对照：全部失败的行为不变（也是 RuntimeError）。"""
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"garbage")
        out = str(tmp_path / "HP_Scan.jpg")
        with pytest.raises(RuntimeError):
            save_manager.save_pages("job_allbad", "jpg", out,
                                    [(str(bad), 0)], [[0]])

    def test_run_save_job_with_empty_page_list_raises(self, tmp_path, isolated):
        """后台入口（run_save_job）同样必须报错，不能返回空结果。"""
        req = save_manager.SaveRequest(
            job_id="job_empty4", ext="jpg", output_path=str(tmp_path / "HP_Scan.jpg"),
            page_specs=[], groups=[])
        with pytest.raises(RuntimeError):
            save_manager.run_save_job(req)
