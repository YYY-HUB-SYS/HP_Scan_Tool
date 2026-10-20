"""
save_manager 单元测试 — HP Scan Tool v4.0
覆盖: 落盘格式 / 分组命名 / 组间不覆盖 / 额外目录复制 / 缓存清理 / 历史写入
"""

import json
import os

import pytest
from PIL import Image

import cache_manager
import history_manager
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
