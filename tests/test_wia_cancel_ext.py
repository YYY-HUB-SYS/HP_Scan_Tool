"""
WIA 路径的取消语义 + ext 归一化回归测试 — HP Scan Tool v4.0

锁死 F-4（Minor）：WIA 是三条引擎路径里**唯一**不可中断的
------------------------------------------------------------------
eSCL 有 `cancel_event`、监听有 `cancel_event`，WIA 全文 0 处 ⇒
GUI 的取消按钮对 WIA 是**假取消**（COM 的 Transfer() 一旦进入，
只能等它自己返回）。
⇒ 修复：wia_scan / try_wia_scan 接受可选 `cancel_event`，
  在耗时点（连接前 / 每个扫描项前后 / Transfer 返回后 / 格式转换前 /
  写盘前 / 读盘后）检查取消，命中即抛 `escl_engine.ScanCancelled`
  —— 与 eSCL 路径**同一个异常类型**，协调器靠它发 scan_cancelled
  而不是 scan_failed。

锁死 F-5（Minor）：WIA 的 ext 原来原样回显
------------------------------------------------------------------
`ext = output_format.lower()` ⇒ 传 "jpeg" 落盘 .jpeg、传 "tif" 落盘 .tif，
而 eSCL 路径按 REQUEST_FORMAT 归一成 .jpg / .tiff ⇒ 同一用户意图在两条
引擎路径落盘的扩展名不同。⇒ 修复：WIA 侧复用同一张 REQUEST_FORMAT。

★ 全部用**假 COM 对象**验证（不跑真机扫描、不出纸、不碰真实设备）。
"""

import io
import threading
import time

import pytest
from PIL import Image

import escl_engine
import wia_engine


def _jpeg_bytes(width=40, height=30):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "JPEG")
    return buf.getvalue()


class _FakeFileDataResult:
    def __init__(self, binary):
        self.BinaryData = binary


class _FakeFileData:
    def __init__(self, binary):
        self._binary = binary

    def GetFile(self, FormatID=None):
        return _FakeFileDataResult(self._binary)


class _FakeWiaImage:
    """假 COM 图像对象：SaveFile 写出 jpeg 字节（模拟 WIA 正常写文件）。"""

    def __init__(self, jpeg_bytes, save_fails=False):
        self.FileData = _FakeFileData(jpeg_bytes)
        self._jpeg = jpeg_bytes
        self._save_fails = save_fails
        self.saved_paths = []

    def SaveFile(self, path):
        self.saved_paths.append(path)
        if self._save_fails:
            raise RuntimeError("WIA 不支持该扩展名")
        with open(path, "wb") as f:
            f.write(self._jpeg)


class _FakeItem:
    def __init__(self, img, on_transfer=None):
        self._img = img
        self._on_transfer = on_transfer
        self.Properties = []
        self.transfer_count = 0

    def Transfer(self):
        self.transfer_count += 1
        if self._on_transfer is not None:
            self._on_transfer()
        return self._img


class _FakeDevice:
    def __init__(self, items):
        self.Items = items


def _install_device(monkeypatch, device):
    monkeypatch.setattr(wia_engine, "_connect_device", lambda device_id: device)


# ── F-4：取消语义 ────────────────────────────────────────


class TestWiaCancellation:
    def test_cancel_before_start_raises_and_skips_transfer(self, monkeypatch):
        """取消信号已置位：立刻抛 ScanCancelled，且**根本不调用** Transfer。"""
        img = _FakeWiaImage(_jpeg_bytes())
        item = _FakeItem(img)
        _install_device(monkeypatch, _FakeDevice([item]))

        event = threading.Event()
        event.set()

        with pytest.raises(escl_engine.ScanCancelled):
            wia_engine.wia_scan(device_id="x", output_format="jpg",
                                 cancel_event=event)

        assert item.transfer_count == 0, "已取消时不该再驱动设备"
        assert img.saved_paths == [], "已取消时不该落盘"

    def test_cancel_is_bounded_time(self, monkeypatch):
        """取消后必须在**有界时间内**返回，而不是跑完整个流程。"""
        img = _FakeWiaImage(_jpeg_bytes())
        item = _FakeItem(img)
        _install_device(monkeypatch, _FakeDevice([item]))

        event = threading.Event()
        event.set()

        started = time.monotonic()
        with pytest.raises(escl_engine.ScanCancelled):
            wia_engine.wia_scan(device_id="x", output_format="jpg",
                                 cancel_event=event)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"取消必须在有界时间内生效，实际 {elapsed:.3f}s"
        assert item.transfer_count == 0

    def test_cancel_during_transfer_is_honoured(self, monkeypatch):
        """Transfer() 返回后立刻查取消：不再进入保存流程。"""
        img = _FakeWiaImage(_jpeg_bytes())
        event = threading.Event()
        item = _FakeItem(img, on_transfer=event.set)
        _install_device(monkeypatch, _FakeDevice([item]))

        with pytest.raises(escl_engine.ScanCancelled):
            wia_engine.wia_scan(device_id="x", output_format="jpg",
                                 cancel_event=event)

        assert item.transfer_count == 1
        assert img.saved_paths == [], "取消后不该再保存文件"

    def test_try_wia_scan_propagates_cancel(self, monkeypatch):
        """try_wia_scan 遇到取消要**抛**，不能吞成 None（取消 ≠ 失败）。"""
        event = threading.Event()
        event.set()

        monkeypatch.setattr(wia_engine, "list_wia_scanners",
                            lambda: [{"id": "dev1", "name": "fake"}])
        monkeypatch.setattr(wia_engine, "_connect_device",
                            lambda device_id: _FakeDevice([]))

        with pytest.raises(escl_engine.ScanCancelled):
            wia_engine.try_wia_scan(output_format="jpg", cancel_event=event)

    def test_no_cancel_event_keeps_old_behaviour(self, monkeypatch):
        """不传 cancel_event：行为与改造前一致（正常返回字节）。"""
        img = _FakeWiaImage(_jpeg_bytes())
        item = _FakeItem(img)
        _install_device(monkeypatch, _FakeDevice([item]))

        data, ext = wia_engine.wia_scan(device_id="x", output_format="jpg")

        assert ext == "jpg"
        assert data[:2] == b"\xff\xd8"
        assert item.transfer_count == 1

    def test_cancel_exception_is_the_same_type_as_escl_path(self):
        """WIA 抛的取消异常必须与 eSCL 路径**同一个类型**。

        协调器的 `except ScanCancelled` 只认它自己那个类（scan_coordinator.py
        不在本片所有权内）⇒ 引擎侧统一用 escl_engine.ScanCancelled，
        协调器侧需要配套改一行（见核销报告）。
        """
        assert wia_engine.ScanCancelled is escl_engine.ScanCancelled
        assert issubclass(escl_engine.ScanJobCancelled, escl_engine.ScanCancelled)
        assert issubclass(escl_engine.ScanCancelled, RuntimeError)


# ── F-5：ext 归一化与 eSCL 路径一致 ───────────────────────


class TestExtNormalization:
    @pytest.mark.parametrize("alias,expected", [
        ("jpg", "jpg"), ("jpeg", "jpg"), ("JPEG", "jpg"),
        ("png", "png"), ("PNG", "png"),
        ("tiff", "tiff"), ("tif", "tiff"), ("TIF", "tiff"),
        ("pdf", "pdf"),
    ])
    def test_normalize_ext_matches_escl_table(self, alias, expected):
        """WIA 的 ext 归一规则必须与 eSCL 路径（REQUEST_FORMAT）**逐字一致**。"""
        assert wia_engine._normalize_ext(alias) == expected
        assert expected == escl_engine.REQUEST_FORMAT[alias.lower()][1]

    @pytest.mark.parametrize("fmt", ["jpg", "jpeg", "png", "tiff", "tif", "pdf"])
    def test_wia_scan_returns_escl_normalized_ext(self, monkeypatch, fmt):
        """wia_scan 返回的 ext 必须与 eSCL 路径对同一格式的返回值一致。"""
        img = _FakeWiaImage(_jpeg_bytes())
        _install_device(monkeypatch, _FakeDevice([_FakeItem(img)]))

        _data, ext = wia_engine.wia_scan(device_id="x", output_format=fmt)

        assert ext == escl_engine.REQUEST_FORMAT[fmt][1]

    def test_tif_uses_tiff_wia_guid(self, monkeypatch):
        """'tif' 归一成 'tiff' 后，WIA 格式 GUID 查得到（原来 'tif' 查不到）。"""
        img = _FakeWiaImage(_jpeg_bytes())
        _install_device(monkeypatch, _FakeDevice([_FakeItem(img)]))

        _data, ext = wia_engine.wia_scan(device_id="x", output_format="tif")

        assert ext == "tiff"
        assert img.saved_paths, "应当调用过 SaveFile"
        assert img.saved_paths[0].endswith(".tiff"), \
            f"临时文件后缀应当是 .tiff，实际 {img.saved_paths[0]}"
        assert "tiff" in wia_engine._WIA_FORMAT_GUID

    def test_jpeg_alias_lands_as_jpg(self, monkeypatch):
        """'jpeg' 归一成 'jpg'：临时文件后缀也是 .jpg（不是 .jpeg）。"""
        img = _FakeWiaImage(_jpeg_bytes())
        _install_device(monkeypatch, _FakeDevice([_FakeItem(img)]))

        _data, ext = wia_engine.wia_scan(device_id="x", output_format="jpeg")

        assert ext == "jpg"
        assert img.saved_paths[0].endswith(".jpg")
