"""
tools/verify_scan.py 单元测试 — HP Scan Tool v4.0

覆盖（N-5 / N-6）：
  - 缓存页损坏 ⇒ 退出码 **3**（不是 1）+ report.json 仍写出（含失败原因）
    + 缓存 job 目录被清理（不留孤儿）
  - 多页且各页 ext 不一致 ⇒ 落盘**文件名与内容格式一致**
  - 既有退出码契约不回归：页数不符 ⇒ 3；正常成功 ⇒ 0；探测不到 ⇒ 2

★ 全程 mock：不连网、不碰真机、不写真实用户缓存目录（缓存根重定向到 tmp）。
"""

import importlib.util
import io
import json
import os
import sys

import pytest
from PIL import Image

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_verify_scan():
    """按路径加载 tools/verify_scan.py（它不是包，不能直接 import）"""
    path = os.path.join(PROJECT_ROOT, "tools", "verify_scan.py")
    spec = importlib.util.spec_from_file_location("verify_scan_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_scan = _load_verify_scan()


def _jpeg_bytes(size=(120, 160)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="JPEG")
    return buf.getvalue()


def _png_bytes(size=(120, 160)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return buf.getvalue()


# PIL 格式 → 合法的文件扩展名
_VALID_EXT = {
    "JPEG": {"jpg", "jpeg"},
    "PNG": {"png"},
    "TIFF": {"tiff", "tif"},
    "BMP": {"bmp"},
    "GIF": {"gif"},
    "WEBP": {"webp"},
}


@pytest.fixture
def fake_device(monkeypatch):
    """把探测 / 能力查询 / 扫描全部 mock 掉（不连网、不碰真机）"""
    monkeypatch.setattr(verify_scan, "probe_escl",
                        lambda ip, timeout=5.0: "http://192.0.2.1/eSCL")
    monkeypatch.setattr(verify_scan, "fetch_capabilities",
                        lambda scanner, timeout=5.0: scanner)


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["verify_scan.py"] + argv)
    return verify_scan.main()


def _job_dirs(cache_root):
    return [d for d in os.listdir(cache_root)
            if os.path.isdir(os.path.join(cache_root, d))]


def _read_report(out_dir):
    with open(os.path.join(str(out_dir), "report.json"), "r", encoding="utf-8") as f:
        return json.load(f)


# ── N-5：损坏页 ──────────────────────────────────────────────


class TestCorruptPage:
    def test_corrupt_page_exits_3_not_1(self, fake_device, monkeypatch,
                                        isolated_cache, tmp_path):
        monkeypatch.setattr(verify_scan, "execute_scan",
                            lambda **kw: (b"NOT-A-JPEG", "jpg"))
        out = tmp_path / "out"
        rc = _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                     "--out", str(out)])
        assert rc == 3, "损坏页必须走「页数不符」同一条失败路径（退出码 3）"

    def test_corrupt_page_still_writes_report_with_reason(self, fake_device,
                                                           monkeypatch,
                                                           isolated_cache,
                                                           tmp_path):
        monkeypatch.setattr(verify_scan, "execute_scan",
                            lambda **kw: (b"NOT-A-JPEG", "jpg"))
        out = tmp_path / "out"
        _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                "--out", str(out)])

        report_path = out / "report.json"
        assert report_path.exists(), "失败也必须写 report.json（否则丢掉对照数据）"
        report = _read_report(out)
        assert report["failures"], "report.json 必须记录失败原因"
        assert report["failures"][0]["stage"] == "decode"
        assert report["error"], "必须有一行失败原因摘要"
        assert report["pages"][0].get("error"), "损坏页本身必须记下错误"

    def test_corrupt_page_cleans_cache_job_dir(self, fake_device, monkeypatch,
                                               isolated_cache, tmp_path):
        monkeypatch.setattr(verify_scan, "execute_scan",
                            lambda **kw: (b"NOT-A-JPEG", "jpg"))
        out = tmp_path / "out"
        _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                "--out", str(out)])
        assert _job_dirs(isolated_cache) == [], "缓存 job 目录必须被清理（不留孤儿）"

    def test_scan_error_still_cleans_cache_and_writes_report(self, fake_device,
                                                             monkeypatch,
                                                             isolated_cache,
                                                             tmp_path):
        def boom(**kw):
            raise RuntimeError("设备无响应")

        monkeypatch.setattr(verify_scan, "execute_scan", boom)
        out = tmp_path / "out"
        rc = _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                     "--out", str(out)])
        assert rc == 3
        report = _read_report(out)
        assert report["failures"][0]["stage"] == "scan"
        assert _job_dirs(isolated_cache) == []


# ── N-6：文件名与内容格式一致 ───────────────────────────────


class TestSavedFileNaming:
    def test_mixed_ext_pages_name_matches_content(self, fake_device, monkeypatch,
                                                   isolated_cache, tmp_path):
        monkeypatch.setattr(verify_scan, "execute_multipage_scan",
                            lambda **kw: [(_jpeg_bytes(), "jpg"), (_png_bytes(), "png")])
        out = tmp_path / "out"
        rc = _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "2",
                                     "--out", str(out), "--source", "Feeder"])
        assert rc == 0

        files = sorted(out.glob("HP_Scan_*_*"))
        assert len(files) == 2
        seen_ext = set()
        for f in files:
            with Image.open(f) as im:
                fmt = im.format
            ext = f.suffix.lstrip(".").lower()
            assert ext in _VALID_EXT[fmt], \
                "文件名扩展名 %r 与内容格式 %r 不一致: %s" % (ext, fmt, f.name)
            seen_ext.add(ext)
        assert seen_ext == {"jpg", "png"}, \
            "每页按自己的实际格式命名（不能全用最后一页的 ext）: %s" % seen_ext

    def test_report_records_per_page_formats(self, fake_device, monkeypatch,
                                              isolated_cache, tmp_path):
        monkeypatch.setattr(verify_scan, "execute_multipage_scan",
                            lambda **kw: [(_jpeg_bytes(), "jpg"), (_png_bytes(), "png")])
        out = tmp_path / "out"
        _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "2",
                                "--out", str(out), "--source", "Feeder"])

        report = _read_report(out)
        assert report["result"]["exts"] == ["jpg", "png"]
        assert report["result"]["ext"] == "mixed"
        assert [p["format"] for p in report["pages"]] == ["JPEG", "PNG"]
        assert [p["saved_file"].rsplit(".", 1)[1] for p in report["pages"]] == \
            ["jpg", "png"]

    def test_single_page_keeps_jpg_naming(self, fake_device, monkeypatch,
                                          isolated_cache, tmp_path):
        monkeypatch.setattr(verify_scan, "execute_scan",
                            lambda **kw: (_jpeg_bytes(), "jpg"))
        out = tmp_path / "out"
        rc = _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                     "--out", str(out)])
        assert rc == 0
        files = sorted(out.glob("HP_Scan_*_*"))
        assert len(files) == 1
        assert files[0].suffix == ".jpg"
        with Image.open(files[0]) as im:
            assert im.format == "JPEG"
        report = _read_report(out)
        assert report["result"]["ext"] == "jpg"


# ── 既有退出码契约不回归 ────────────────────────────────────


class TestExitCodeContract:
    def test_success_returns_0(self, fake_device, monkeypatch, isolated_cache,
                                tmp_path):
        monkeypatch.setattr(verify_scan, "execute_scan",
                            lambda **kw: (_jpeg_bytes(), "jpg"))
        out = tmp_path / "out"
        assert _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                       "--out", str(out)]) == 0

    def test_page_count_mismatch_returns_3(self, fake_device, monkeypatch,
                                            isolated_cache, tmp_path):
        monkeypatch.setattr(verify_scan, "execute_scan",
                            lambda **kw: (_jpeg_bytes(), "jpg"))
        out = tmp_path / "out"
        assert _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "3",
                                       "--out", str(out)]) == 3
        report = _read_report(out)
        assert report["params"]["requested_pages"] == 3
        assert report["result"]["page_count"] == 1

    def test_probe_failure_returns_2_without_report(self, monkeypatch,
                                                     isolated_cache, tmp_path):
        monkeypatch.setattr(verify_scan, "probe_escl",
                            lambda ip, timeout=5.0: None)
        out = tmp_path / "out"
        assert _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                       "--out", str(out)]) == 2
        assert not (out / "report.json").exists()

    def test_report_json_is_lf_without_bom(self, fake_device, monkeypatch,
                                           isolated_cache, tmp_path):
        monkeypatch.setattr(verify_scan, "execute_scan",
                            lambda **kw: (_jpeg_bytes(), "jpg"))
        out = tmp_path / "out"
        _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                "--out", str(out)])
        raw = (out / "report.json").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "report.json 不能有 BOM"
        assert b"\r\n" not in raw, "report.json 必须是 LF 换行"
