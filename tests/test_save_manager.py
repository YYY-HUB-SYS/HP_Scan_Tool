"""
save_manager 单元测试 — HP Scan Tool v4.0
覆盖: 落盘格式 / 分组命名 / 组间不覆盖 / 额外目录复制 / 缓存清理 / 历史写入
"""

import json
import os

import pytest
from PIL import Image
from pypdf import PdfReader

import cache_manager
import history_manager
import page_meta
import save_manager


@pytest.fixture
def jpg(tmp_path):
    p = tmp_path / "src.jpg"
    Image.new("RGB", (100, 200), "white").save(p, "JPEG")
    return str(p)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把缓存根目录与历史文件重定向到临时目录"""
    cache_root = str(tmp_path / "cache")
    os.makedirs(cache_root, exist_ok=True)
    monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: cache_root)
    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: os.path.join(cache_root, "cache_config.json"))
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(cache_manager, "_active_jobs", set())
    hist = str(tmp_path / "history.json")
    monkeypatch.setattr(history_manager, "_history_file", lambda: hist)
    monkeypatch.setattr(history_manager, "HISTORY_FILE", None)
    return cache_root, hist


class TestBuildOutputName:
    def test_single_group_pdf_keeps_original(self):
        assert save_manager.build_output_name("C:/out/HP_Scan", "pdf", 1, 1, 1) == "C:/out/HP_Scan.pdf"

    def test_single_group_image_keeps_original(self):
        assert save_manager.build_output_name("C:/out/HP_Scan", "jpg", 1, 1, 1) == "C:/out/HP_Scan_001.jpg"

    def test_multi_group_pdf_gets_group_index(self):
        assert save_manager.build_output_name("C:/out/HP_Scan", "pdf", 2, 1, 1) == "C:/out/HP_Scan_001.pdf"
        assert save_manager.build_output_name("C:/out/HP_Scan", "pdf", 2, 2, 1) == "C:/out/HP_Scan_002.pdf"

    def test_multi_group_image_gets_group_and_seq(self):
        assert save_manager.build_output_name("C:/out/HP_Scan", "jpg", 2, 1, 1) == "C:/out/HP_Scan_001_001.jpg"
        assert save_manager.build_output_name("C:/out/HP_Scan", "jpg", 2, 2, 3) == "C:/out/HP_Scan_002_003.jpg"


class TestSaveSingle:
    def test_saves_jpg(self, tmp_path, jpg, isolated):
        out = str(tmp_path / "out.jpg")
        saved = save_manager.save_single(jpg, "jpg", out)
        assert os.path.exists(saved)
        with Image.open(saved) as im:
            assert im.size == (100, 200)

    def test_extension_corrected_when_mismatch(self, tmp_path, jpg, isolated):
        out = str(tmp_path / "out.png")
        saved = save_manager.save_single(jpg, "jpg", out)
        assert saved.endswith(".jpg")
        assert os.path.exists(saved)

    def test_rotation_applied(self, tmp_path, jpg, isolated):
        out = str(tmp_path / "out.jpg")
        saved = save_manager.save_single(jpg, "jpg", out, rotation=90)
        with Image.open(saved) as im:
            assert im.size == (200, 100)


class TestSavePages:
    def test_multi_group_pdf_does_not_overwrite(self, tmp_path, jpg, isolated):
        base = str(tmp_path / "HP_Scan.pdf")
        specs = [(jpg, 0), (jpg, 0)]
        groups = [[0], [1]]
        files = save_manager.save_pages("job1", "pdf", base, specs, groups)
        assert len(files) == 2
        assert files[0] != files[1]
        assert files[0].endswith("_001.pdf")
        assert files[1].endswith("_002.pdf")
        assert os.path.getsize(files[0]) > 0
        assert os.path.getsize(files[1]) > 0

    def test_multi_group_image_does_not_overwrite(self, tmp_path, jpg, isolated):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(jpg, 0), (jpg, 0)]
        groups = [[0], [1]]
        files = save_manager.save_pages("job1", "jpg", base, specs, groups)
        assert len(files) == 2
        assert files[0].endswith("_001_001.jpg")
        assert files[1].endswith("_002_001.jpg")
        assert os.path.getsize(files[0]) > 0
        assert os.path.getsize(files[1]) > 0

    def test_single_group_image_numbering(self, tmp_path, jpg, isolated):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(jpg, 0), (jpg, 0)]
        files = save_manager.save_pages("job1", "jpg", base, specs, [[0, 1]])
        assert len(files) == 2
        assert files[0].endswith("_001.jpg")
        assert files[1].endswith("_002.jpg")

    def test_single_group_pdf_single_file(self, tmp_path, jpg, isolated):
        base = str(tmp_path / "HP_Scan.pdf")
        specs = [(jpg, 0), (jpg, 0)]
        files = save_manager.save_pages("job1", "pdf", base, specs, [[0, 1]])
        assert len(files) == 1
        assert files[0].endswith("HP_Scan.pdf")


class TestFinalize:
    def test_extra_dirs_copied(self, tmp_path, jpg, isolated):
        extra = str(tmp_path / "extra")
        out = str(tmp_path / "out.jpg")
        saved = save_manager.save_single(jpg, "jpg", out, extra_dirs=[extra])
        assert os.path.exists(os.path.join(extra, os.path.basename(saved)))

    def test_history_written(self, tmp_path, jpg, isolated):
        _, hist = isolated
        out = str(tmp_path / "out.jpg")
        save_manager.save_single(jpg, "jpg", out,
                                 device_name="HP M232", device_ip="1.2.3.4")
        with open(hist, encoding="utf-8") as f:
            records = json.load(f)
        assert len(records) == 1
        assert records[0]["device_name"] == "HP M232"
        assert records[0]["device_ip"] == "1.2.3.4"
        assert records[0]["page_count"] == 1

    def test_cache_job_removed(self, tmp_path, jpg, isolated):
        cache_root, _ = isolated
        job_id = "job_test"
        cache_manager.register_job(job_id)
        cache_manager.write_page(job_id, 1, b"data", "jpg")
        assert os.path.isdir(os.path.join(cache_root, job_id))
        out = str(tmp_path / "out.jpg")
        save_manager.save_single(jpg, "jpg", out, job_id=job_id)
        assert not os.path.isdir(os.path.join(cache_root, job_id))

    def test_cache_removed_when_source_is_the_cache_file(self, tmp_path, jpg, isolated):
        """保存源就是缓存文件本身时，保存后缓存目录也必须被真正删除。

        守护的是用户可见行为："保存完成后，缓存目录确实被删掉"
        （即 cache_manager.remove_job 确实生效了）。

        注意：本用例**不能**证明 "with Image.open(...) 是句柄释放的必要条件"。
        Pillow 13.0.0 的 ImageFile.load() 在 _exclusive_fp 且
        _close_exclusive_fp_after_loading 时会执行 fp.close()，而 copy() /
        rotate() / convert() / save() 全部会触发 load —— 图片只要被读过一次，
        独占文件句柄就已释放。实测把 save_manager 里 3 处 with 全部去掉，
        本用例仍然通过（review 变异测试 M1：16 项全绿）。

        改造前 GUI 内 13 处 Image.open 不关句柄（hp_scan_gui_pyside.py），
        其中 4 处紧跟 remove_job。
        """
        cache_root, _ = isolated
        job_id = "job_self"
        cache_manager.register_job(job_id)
        with open(jpg, "rb") as f:
            data = f.read()
        cache_path = cache_manager.write_page(job_id, 1, data, "jpg")
        out = str(tmp_path / "out.jpg")
        save_manager.save_single(cache_path, "jpg", out, job_id=job_id)
        assert not os.path.isdir(os.path.join(cache_root, job_id))

    def test_cache_removed_after_pdf_save_from_cache(self, tmp_path, jpg, isolated):
        """PDF 分支保存后，缓存目录也必须被真正删除。

        守护的是用户可见行为："PDF 保存完成后，缓存目录确实被删掉"。
        同样不能证明 "with 是句柄释放的必要条件"——Pillow 在 load() 之后
        即关闭独占句柄，去掉 with 本用例依然通过（见 review 变异 M1）。
        """
        cache_root, _ = isolated
        job_id = "job_pdf"
        cache_manager.register_job(job_id)
        with open(jpg, "rb") as f:
            data = f.read()
        cache_path = cache_manager.write_page(job_id, 1, data, "jpg")
        base = str(tmp_path / "HP_Scan.pdf")
        save_manager.save_pages(job_id, "pdf", base, [(cache_path, 0)], [[0]])
        assert not os.path.isdir(os.path.join(cache_root, job_id))


class TestRequestFormat:
    """请求层与落盘层对 PDF 的理解必须一致（C-1 根因）"""

    def test_request_format_table(self):
        from escl_engine import REQUEST_FORMAT
        assert REQUEST_FORMAT["jpg"] == ("image/jpeg", "jpg")
        assert REQUEST_FORMAT["jpeg"] == ("image/jpeg", "jpg")
        assert REQUEST_FORMAT["png"] == ("image/png", "png")
        assert REQUEST_FORMAT["tiff"] == ("image/tiff", "tiff")
        # 选 PDF 时打印机只出图片，PDF 落盘时本地合成
        assert REQUEST_FORMAT["pdf"] == ("image/jpeg", "pdf")

    def test_format_mime_kept_untouched(self):
        """FORMAT_MIME / MIME_EXT 保留不动（可能有其它调用方引用）"""
        from escl_engine import FORMAT_MIME, MIME_EXT
        assert FORMAT_MIME["pdf"] == "application/pdf"
        assert MIME_EXT["application/pdf"] == "pdf"
        assert MIME_EXT["image/jpeg"] == "jpg"

    def test_scan_job_requests_image_for_pdf(self):
        """选 PDF 时向打印机请求 image/jpeg（不是 application/pdf），落盘扩展名是 pdf"""
        import escl_engine

        captured = {}

        class _Resp:
            status_code = 200
            headers = {"Location": "http://1.2.3.4/eSCL/ScanJobs/1"}
            text = ""

        class _Session:
            def post(self, url, data=None, headers=None, timeout=None):
                captured["xml"] = data
                return _Resp()

        scanner = escl_engine.ScannerInfo(name="t", ip="1.2.3.4")
        scanner.escl_url = "http://1.2.3.4/eSCL/"
        _base, _job_uri, ext = escl_engine._create_scan_job(
            _Session(), scanner, 300, "RGB24", "pdf", "Platen", False)
        assert ext == "pdf"
        assert "<pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>" in captured["xml"]
        assert "application/pdf" not in captured["xml"]


class TestPdfOutput:
    """选 PDF 的完整链路：缓存页 → 本地合成 → 可打开的 PDF"""

    def test_pdf_output_is_readable_pdf(self, tmp_path, jpg, isolated):
        """C-1 回归护栏：选 PDF 时 save_single 必须产出可被 pypdf 打开的 PDF。

        改造前：打印机直出 PDF 字节 → 落盘层按图片处理 → 单页抛
        UnidentifiedImageError、多页分支的 except 把异常吞掉 →
        saved_files=[] → 对话框静默关闭，提示成功但产出 0 个文件。
        """
        out = str(tmp_path / "out.pdf")
        saved = save_manager.save_single(jpg, "pdf", out)
        assert saved.endswith(".pdf")
        assert os.path.exists(saved)
        assert os.path.getsize(saved) > 0
        reader = PdfReader(saved)
        assert len(reader.pages) == 1

    def test_single_and_multi_pdf_same_page_size(self, tmp_path, jpg, isolated):
        """I-13 回归护栏：单页与多页 PDF 的第一页 mediabox 必须相等。

        改造前单页走 _save_a4_pdf（A4 归一）、多页走 images[0].save(save_all=True)
        （不归一），同一次扫描里两种输出的页面尺寸不一致。
        """
        single = save_manager.save_single(jpg, "pdf", str(tmp_path / "single.pdf"))
        multi_files = save_manager.save_pages(
            "job_i13", "pdf", str(tmp_path / "multi.pdf"),
            [(jpg, 0), (jpg, 0)], [[0, 1]])
        assert len(multi_files) == 1
        sb = PdfReader(single).pages[0].mediabox
        mb = PdfReader(multi_files[0]).pages[0].mediabox
        assert tuple(sb) == tuple(mb)
        # A4 @300dpi → 595.2 x 841.68 pt
        assert abs(float(sb.width) - 595.2) < 0.5
        assert abs(float(sb.height) - 841.68) < 0.5

    def test_pdf_from_a4_sized_source(self, tmp_path, isolated):
        """源图尺寸恰好等于 A4@300dpi 时也必须能合成。

        这不是边角情况：本机平板扫描基准就是 2480x3507（正是 A4@300dpi），
        画布归一时返回值可能与入参是同一个对象，close() 顺序错了就会
        "Operation on closed image"。
        """
        p = tmp_path / "a4.jpg"
        Image.new("RGB", (2480, 3507), "white").save(p, "JPEG")
        out = str(tmp_path / "a4.pdf")
        saved = save_manager.save_single(str(p), "pdf", out)
        assert os.path.getsize(saved) > 0
        assert len(PdfReader(saved).pages) == 1
        mb = PdfReader(saved).pages[0].mediabox
        assert abs(float(mb.width) - 595.2) < 0.5
        assert abs(float(mb.height) - 841.68) < 0.5

    def test_multi_group_pdf_valid_and_not_overwritten(self, tmp_path, jpg, isolated):
        """多组 PDF 仍不覆盖，且每一组都是可打开的 1 页 PDF。"""
        base = str(tmp_path / "HP_Scan.pdf")
        files = save_manager.save_pages("job_g", "pdf", base,
                                        [(jpg, 0), (jpg, 0)], [[0], [1]])
        assert len(files) == 2
        assert files[0] != files[1]
        assert files[0].endswith("_001.pdf")
        assert files[1].endswith("_002.pdf")
        for f in files:
            assert os.path.getsize(f) > 0
            assert len(PdfReader(f).pages) == 1

    def test_pdf_writer_appends_page_by_page(self, tmp_path, jpg, isolated,
                                              monkeypatch):
        """流式合成：PdfWriter.append 必须逐页调用，而不是一次性传列表。"""
        calls = []
        real_writer = save_manager.PdfWriter

        class _RecordingWriter(real_writer):
            def append(self, fileobj, *args, **kwargs):
                calls.append((fileobj, os.path.exists(fileobj)))
                return super().append(fileobj, *args, **kwargs)

        monkeypatch.setattr(save_manager, "PdfWriter", _RecordingWriter)
        files = save_manager.save_pages(
            "job_stream", "pdf", str(tmp_path / "stream.pdf"),
            [(jpg, 0)] * 5, [[0, 1, 2, 3, 4]])
        assert len(files) == 1
        assert len(calls) == 5, "PdfWriter.append 必须逐页调用"
        for fileobj, existed in calls:
            assert isinstance(fileobj, str), "append 必须传单个文件路径，不能传列表"
            assert existed, "被 append 的单页 PDF 临时文件必须在调用时存在"
        assert len(PdfReader(files[0]).pages) == 5

    def test_only_one_page_kept_in_memory(self, tmp_path, jpg, isolated,
                                          monkeypatch):
        """内存护栏：合成过程中同时存活的 PIL 图像数不随页数增长。

        改造前 save_pages 把全部页一次性 Image.open 进 images 列表
        （300dpi 约 26MB/页 → 400 页约 10.4GB，必然 OOM）。
        """
        import gc

        counts = []
        real_canvas = save_manager._a4_canvas

        def _tracked_canvas(img, *args, **kwargs):
            counts.append(sum(1 for o in gc.get_objects()
                               if isinstance(o, Image.Image)))
            return real_canvas(img, *args, **kwargs)

        monkeypatch.setattr(save_manager, "_a4_canvas", _tracked_canvas)
        save_manager.save_pages("job_mem", "pdf", str(tmp_path / "mem.pdf"),
                                [(jpg, 0)] * 8, [[0, 1, 2, 3, 4, 5, 6, 7]])
        assert len(counts) == 8
        spread = max(counts) - min(counts)
        assert spread <= 4, f"存活图像数随页数增长（旧实现会线性增长）: {counts}"

    def test_twenty_pages_pdf(self, tmp_path, jpg, isolated):
        """20 页规模：合成成功、页数正确、耗时可接受。"""
        import time
        t0 = time.time()
        files = save_manager.save_pages(
            "job_20", "pdf", str(tmp_path / "big.pdf"),
            [(jpg, 0)] * 20,
            [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]])
        dt = time.time() - t0
        assert len(files) == 1
        assert len(PdfReader(files[0]).pages) == 20
        assert dt < 60, f"20 页合成耗时过长: {dt:.1f}s"

    def test_tmp_files_cleaned(self, tmp_path, jpg, isolated, monkeypatch):
        """阶段一的单页 PDF 临时文件用完即删，系统临时目录不留残留。"""
        import tempfile

        created = []
        real_mkdtemp = tempfile.mkdtemp

        def _fake_mkdtemp(*args, **kwargs):
            kwargs.pop("prefix", None)
            d = real_mkdtemp(dir=str(tmp_path))
            created.append(d)
            return d

        monkeypatch.setattr(save_manager.tempfile, "mkdtemp", _fake_mkdtemp)
        files = save_manager.save_pages("job_tmp", "pdf", str(tmp_path / "t.pdf"),
                                        [(jpg, 0)] * 3, [[0, 1, 2]])
        assert len(files) == 1
        assert created, "应当创建过临时目录"
        for d in created:
            assert not os.path.exists(d), f"临时目录未清理: {d}"
        leftovers = [p for p in os.listdir(tempfile.gettempdir())
                     if p.startswith("hp_scan_pdf_")]
        assert not leftovers, f"系统临时目录有残留: {leftovers}"

    def test_page_meta_removed_after_save(self, tmp_path, jpg, isolated):
        """保存后该 job 的页元数据必须一并清理。"""
        cache_root, _ = isolated
        store = page_meta.PageMetaStore(cache_root)
        store.set_page("job_meta", 1, page_meta.PageMeta(
            cache_path=jpg, width=100, height=200))
        assert store.get_job("job_meta")
        save_manager.save_single(jpg, "jpg", str(tmp_path / "out.jpg"),
                                 job_id="job_meta")
        assert store.get_job("job_meta") == {}
