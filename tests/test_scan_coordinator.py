"""
ScanCoordinator 单元测试 — HP Scan Tool v4.0
覆盖: ADF 来源决策 / 单页与多页分支 / 缓存写入 / 取消透传 / 错误与取消信号
"""

import os
import threading

import pytest
from unittest import mock

import scan_coordinator
from scan_coordinator import ScanCancelled, ScanCoordinator, ScanResult


@pytest.fixture
def coordinator():
    c = ScanCoordinator()
    c.cfg = {}
    return c


@pytest.fixture
def scanner():
    from escl_engine import ScannerInfo
    s = ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    s.has_adf = False
    return s


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    root = str(tmp_path / "cache")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_default_cache_root", lambda: root)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_config_cache", None)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_active_jobs", set())
    return root


def _params(tmp_path, **kw):
    p = {
        "resolution": 300,
        "color_mode_ui": "彩色",
        "output_format": "jpg",
        "source": "平板",
        "duplex": False,
        "output_dir": str(tmp_path),
    }
    p.update(kw)
    return p


class TestResolveSource:
    def test_no_adf_capability_returns_platen(self, coordinator, scanner):
        scanner.has_adf = False
        assert coordinator.resolve_source(scanner, "Feeder") == ("Platen", False)

    def test_no_escl_url_returns_platen(self, coordinator, scanner):
        scanner.has_adf = True
        scanner.escl_url = ""
        assert coordinator.resolve_source(scanner, "Feeder") == ("Platen", False)

    def test_adf_loaded_returns_feeder_for_auto(self, coordinator, scanner):
        scanner.has_adf = True
        with mock.patch("scan_coordinator.get_scanner_status",
                        return_value={"state": "Idle", "adf_loaded": True}):
            assert coordinator.resolve_source(scanner, "Auto") == ("Feeder", True)

    def test_platen_request_is_respected(self, coordinator, scanner):
        """用户显式选平板时，即使 ADF 有纸也不得被切成 Feeder，且不发起探测。

        改造前的 GUI `_detect_adf_source` 是"选平板就是平板"，这里必须保持一致，
        否则用户选了平板却被拿输稿器扫，是行为回归。
        """
        scanner.has_adf = True
        with mock.patch("scan_coordinator.get_scanner_status",
                        return_value={"state": "Idle", "adf_loaded": True}) as probe:
            assert coordinator.resolve_source(scanner, "Platen") == ("Platen", False)
            assert probe.call_count == 0

    def test_adf_empty_returns_platen(self, coordinator, scanner):
        scanner.has_adf = True
        with mock.patch("scan_coordinator.get_scanner_status",
                        return_value={"state": "Idle", "adf_loaded": False}):
            assert coordinator.resolve_source(scanner, "Feeder") == ("Platen", False)

    def test_status_failure_keeps_requested_source(self, coordinator, scanner):
        scanner.has_adf = True
        with mock.patch("scan_coordinator.get_scanner_status",
                        side_effect=RuntimeError("boom")):
            assert coordinator.resolve_source(scanner, "Feeder") == ("Feeder", False)


class TestRunScanSync:
    def test_single_page_writes_cache(self, coordinator, scanner, tmp_path, cache_root):
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(b"\xff\xd8fake", "jpg")) as m:
            result = coordinator.run_scan_sync(scanner, _params(tmp_path))
        assert isinstance(result, ScanResult)
        assert result.page_count == 1
        assert result.ext == "jpg"
        assert result.source == "Platen"
        assert result.adf_detected is False
        assert len(result.cached_paths) == 1
        assert os.path.exists(result.cached_paths[0])
        assert result.output_path.endswith(".jpg")

    def test_single_page_passes_cancel_event(self, coordinator, scanner, tmp_path, cache_root):
        captured = {}

        def fake_scan(**kwargs):
            captured.update(kwargs)
            return (b"\xff\xd8fake", "jpg")

        ev = threading.Event()
        with mock.patch("scan_coordinator.execute_scan", side_effect=fake_scan):
            coordinator.run_scan_sync(scanner, _params(tmp_path), cancel_event=ev)
        assert captured["cancel_event"] is ev

    def test_multipage_writes_each_page(self, coordinator, scanner, tmp_path, cache_root):
        scanner.has_adf = True
        pages = [(b"a", "jpg"), (b"b", "jpg"), (b"c", "jpg")]
        with mock.patch("scan_coordinator.get_scanner_status",
                        return_value={"state": "Idle", "adf_loaded": True}), \
             mock.patch("scan_coordinator.execute_multipage_scan", return_value=pages):
            # 必须显式请求输稿器：显式选平板时不再探测，也不会切到 Feeder
            result = coordinator.run_scan_sync(scanner, _params(tmp_path, source="ADF"))
        assert result.page_count == 3
        assert len(result.cached_paths) == 3
        assert result.source == "Feeder"
        assert result.adf_detected is True
        for p in result.cached_paths:
            assert os.path.exists(p)

    def test_engine_error_propagates(self, coordinator, scanner, tmp_path, cache_root):
        with mock.patch("scan_coordinator.execute_scan", side_effect=RuntimeError("扫描超时")):
            with pytest.raises(RuntimeError):
                coordinator.run_scan_sync(scanner, _params(tmp_path))

    def test_no_data_raises_cancelled(self, coordinator, scanner, tmp_path, cache_root):
        with mock.patch("scan_coordinator.execute_scan", return_value=(None, "jpg")):
            with pytest.raises(ScanCancelled):
                coordinator.run_scan_sync(scanner, _params(tmp_path))


class TestSignals:
    def test_failed_signal_emitted(self, coordinator, scanner, tmp_path):
        received = []
        coordinator.scan_failed.connect(lambda jid, err: received.append((jid, err)))
        task = scan_coordinator._ScanTask(coordinator, scanner,
                                          _params(tmp_path), "job_x", threading.Event())
        with mock.patch("scan_coordinator.execute_scan", side_effect=RuntimeError("boom")):
            task.run()
        assert received and received[0][0] == "job_x" and received[0][1] == "boom"

    def test_finished_signal_emitted(self, coordinator, scanner, tmp_path, cache_root):
        received = []
        coordinator.scan_finished.connect(lambda jid, res: received.append((jid, res)))
        task = scan_coordinator._ScanTask(coordinator, scanner,
                                          _params(tmp_path), "job_y", threading.Event())
        with mock.patch("scan_coordinator.execute_scan", return_value=(b"\xff\xd8fake", "jpg")):
            task.run()
        assert received and received[0][0] == "job_y"
        assert received[0][1].page_count == 1

    def test_cancelled_signal_emitted(self, coordinator, scanner, tmp_path, cache_root):
        received = []
        coordinator.scan_cancelled.connect(lambda jid: received.append(jid))
        ev = threading.Event()
        ev.set()
        task = scan_coordinator._ScanTask(coordinator, scanner,
                                          _params(tmp_path), "job_z", ev)
        with mock.patch("scan_coordinator.execute_scan", return_value=(None, "jpg")):
            task.run()
        assert received == ["job_z"]


class TestCancelAndShutdown:
    def test_cancel_scan_sets_event(self, coordinator):
        ev = threading.Event()
        coordinator._cancel_events["j1"] = ev
        coordinator.cancel_scan("j1")
        assert ev.is_set()

    def test_cancel_unknown_job_is_noop(self, coordinator):
        coordinator.cancel_scan("does-not-exist")

    def test_shutdown_sets_all_events(self, coordinator):
        e1, e2 = threading.Event(), threading.Event()
        coordinator._cancel_events["a"] = e1
        coordinator._cancel_events["b"] = e2
        coordinator.shutdown()
        assert e1.is_set() and e2.is_set()
