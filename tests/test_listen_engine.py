"""
listen_engine 单元测试 — HP Scan Tool v4.0
覆盖: 监听模式创建 ScanJob 时 max_pages 的取值。
ADF 必须放大 MaxScanPages，否则 _build_scan_job_xml 只写 MaxScanPages=1，
打印机只吐 1 页。
"""

import threading

import pytest

import listen_engine
from escl_engine import ScannerInfo


@pytest.fixture
def scanner():
    s = ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    s.has_adf = True
    return s


def _patch_create_scan_job(monkeypatch):
    """拦截 listen_engine 实际调用的 _create_scan_job，记录传入参数。

    注意：listen_engine 写的是 `from escl_engine import _create_scan_job`，
    名字在 import 时就绑到了本模块命名空间，所以补丁必须打在
    listen_engine._create_scan_job 上；只打 escl_engine._create_scan_job 拦不到。
    """
    captured = {}

    def fake_create_scan_job(session, scanner, resolution, color_mode,
                             output_format, source, duplex, max_pages=1):
        captured["source"] = source
        captured["max_pages"] = max_pages
        return ("http://1.2.3.4:80/eSCL",
                "http://1.2.3.4:80/eSCL/ScanJobs/1", "jpg")

    monkeypatch.setattr(listen_engine, "_create_scan_job", fake_create_scan_job)
    return captured

# wait_timeout=0 + 已置位的 cancel_event：轮询循环一次都不进，
# 既不会发网络请求，也不会因为时钟回拨意外联网，测试完全离线且确定。
def _no_polling():
    ev = threading.Event()
    ev.set()
    return ev


class TestListenForScanMaxPages:
    def test_feeder_uses_large_max_pages(self, scanner, monkeypatch):
        """监听 + ADF 必须传大于 1 的 max_pages，否则打印机只吐 1 页。"""
        captured = _patch_create_scan_job(monkeypatch)
        listen_engine.listen_for_scan(scanner, source="Feeder",
                                      wait_timeout=0, cancel_event=_no_polling())
        assert captured["source"] == "Feeder"
        assert captured["max_pages"] >= 100, (
            f'Feeder 监听必须放大 MaxScanPages，实际传了 {captured["max_pages"]}')

    def test_platen_uses_single_max_pages(self, scanner, monkeypatch):
        """平板来源保持 max_pages=1，不改变单页语义。"""
        captured = _patch_create_scan_job(monkeypatch)
        listen_engine.listen_for_scan(scanner, source="Platen",
                                      wait_timeout=0, cancel_event=_no_polling())
        assert captured["source"] == "Platen"
        assert captured["max_pages"] == 1


class TestListenForMultipageScanMaxPages:
    def test_feeder_uses_large_max_pages(self, scanner, monkeypatch):
        """多页监听 + ADF 同样必须放大 MaxScanPages。"""
        captured = _patch_create_scan_job(monkeypatch)
        listen_engine.listen_for_multipage_scan(scanner, source="Feeder",
                                                wait_timeout=0, page_timeout=0,
                                                cancel_event=_no_polling())
        assert captured["source"] == "Feeder"
        assert captured["max_pages"] >= 100, (
            f'Feeder 监听必须放大 MaxScanPages，实际传了 {captured["max_pages"]}')

    def test_platen_uses_single_max_pages(self, scanner, monkeypatch):
        """平板来源保持 max_pages=1。"""
        captured = _patch_create_scan_job(monkeypatch)
        listen_engine.listen_for_multipage_scan(scanner, source="Platen",
                                                wait_timeout=0, page_timeout=0,
                                                cancel_event=_no_polling())
        assert captured["source"] == "Platen"
        assert captured["max_pages"] == 1
