"""
tools/verify_scan.py「裁剪前 / 裁剪后」双口径回归测试 — HP Scan Tool v4.0

背景（2026-10-21 口径订正）：
  `tools/verify_scan.py` 的 `_a4_crop` 会**原地覆写**缓存页（等比裁剪成
  A4 画布 2480×3507），随后才采集元数据 ⇒ report.json 里的
  `pages[].width/height` 量到的是**裁剪后**的值，裁剪前的原始扫描幅面
  （2550×3508）在磁盘上根本不存在。D1/D2 文档里那个"2480×3507"硬指标
  因此**只代表 A4 画布口径**，不能当作 app 侧图片产物的行为。

本文件锁死：
  - report.json **同时**报出「裁剪前（原始幅面）」与「裁剪后（A4）」两组尺寸；
  - 脚本**默认行为与退出码语义不变**（默认仍然原地裁剪、仍然 0/2/3）；
  - `--no-a4-crop` 开关存在时，产物保留原始幅面（用于只量原始幅面）。

★ 全程 mock：不连网、不碰真机、不写真实用户数据。
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
    spec = importlib.util.spec_from_file_location("verify_scan_size_basis", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_scan = _load_verify_scan()


def _jpeg_bytes(size=(2550, 3508)):
    """一张原始扫描幅面大小的白图（JPEG 字节）"""
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="JPEG", quality=70)
    return buf.getvalue()


@pytest.fixture
def fake_device(monkeypatch):
    """把探测 / 能力查询 / 扫描全部 mock 掉（不连网、不碰真机）"""
    monkeypatch.setattr(verify_scan, "probe_escl",
                        lambda ip, timeout=5.0: "http://192.0.2.1/eSCL")
    monkeypatch.setattr(verify_scan, "fetch_capabilities",
                        lambda scanner, timeout=5.0: scanner)
    monkeypatch.setattr(verify_scan, "execute_scan",
                        lambda **kw: (_jpeg_bytes(), "jpg"))


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["verify_scan.py"] + argv)
    return verify_scan.main()


def _read_report(out_dir):
    with open(os.path.join(str(out_dir), "report.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _fake_crop_to_a4(img, dpi=300):
    """假的 A4 归一：砍掉一半 ⇒ 裁剪前 / 裁剪后尺寸**可区分**（不依赖真实 A4 尺寸）"""
    w, h = img.size
    return img.crop((0, 0, w // 2, h // 2))


class TestBothSizesAreReported:
    def test_report_has_raw_and_cropped_sizes(self, fake_device, monkeypatch,
                                               isolated_cache, tmp_path):
        """report.json 同时报出裁剪前（原始幅面）与裁剪后（A4）两组尺寸"""
        monkeypatch.setattr(verify_scan, "crop_to_a4", _fake_crop_to_a4)
        out = tmp_path / "out"
        rc = _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                     "--out", str(out)])
        assert rc == 0

        report = _read_report(out)
        page = report["pages"][0]
        assert page["raw_width"] == 2550 and page["raw_height"] == 3508, \
            f"必须报出裁剪前的原始幅面: {page}"
        assert page["width"] == 1275 and page["height"] == 1754, \
            f"必须报出裁剪后的尺寸: {page}"

    def test_saved_artifact_keeps_post_crop_size_by_default(self, fake_device,
                                                             monkeypatch,
                                                             isolated_cache,
                                                             tmp_path):
        """默认行为不变：落盘产物仍是裁剪后的尺寸"""
        monkeypatch.setattr(verify_scan, "crop_to_a4", _fake_crop_to_a4)
        out = tmp_path / "out"
        _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                "--out", str(out)])

        files = sorted(out.glob("HP_Scan_*_*"))
        assert len(files) == 1
        with Image.open(files[0]) as im:
            assert (im.width, im.height) == (1275, 1754)


class TestNoA4CropSwitch:
    def test_switch_keeps_original_size(self, fake_device, monkeypatch,
                                        isolated_cache, tmp_path):
        """--no-a4-crop ⇒ 产物保留原始扫描幅面（只量原始幅面用）"""
        called = []

        def spy(img, dpi=300):
            called.append(dpi)
            return _fake_crop_to_a4(img, dpi)

        monkeypatch.setattr(verify_scan, "crop_to_a4", spy)
        out = tmp_path / "out"
        rc = _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                     "--out", str(out), "--no-a4-crop"])
        assert rc == 0
        assert not called, "--no-a4-crop 时不得调用 A4 归一"

        files = sorted(out.glob("HP_Scan_*_*"))
        assert len(files) == 1
        with Image.open(files[0]) as im:
            assert (im.width, im.height) == (2550, 3508)

        page = _read_report(out)["pages"][0]
        assert page["raw_width"] == 2550 and page["raw_height"] == 3508
        assert page["width"] == 2550 and page["height"] == 3508

    def test_default_still_crops(self, fake_device, monkeypatch,
                                 isolated_cache, tmp_path):
        """不加开关 ⇒ 仍然裁剪（默认行为不变）"""
        called = []

        def spy(img, dpi=300):
            called.append(dpi)
            return _fake_crop_to_a4(img, dpi)

        monkeypatch.setattr(verify_scan, "crop_to_a4", spy)
        out = tmp_path / "out"
        _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                "--out", str(out)])
        assert called, "默认必须仍然做 A4 归一"


class TestExitCodeContractUnchanged:
    def test_success_is_0(self, fake_device, monkeypatch, isolated_cache,
                          tmp_path):
        out = tmp_path / "out"
        assert _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                       "--out", str(out)]) == 0

    def test_page_mismatch_is_3(self, fake_device, monkeypatch, isolated_cache,
                                tmp_path):
        out = tmp_path / "out"
        assert _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "3",
                                       "--out", str(out)]) == 3

    def test_probe_failure_is_2(self, monkeypatch, isolated_cache, tmp_path):
        monkeypatch.setattr(verify_scan, "probe_escl",
                            lambda ip, timeout=5.0: None)
        out = tmp_path / "out"
        assert _run_main(monkeypatch, ["--ip", "192.0.2.1", "--pages", "1",
                                       "--out", str(out)]) == 2
