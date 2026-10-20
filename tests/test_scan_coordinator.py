"""
ScanCoordinator 单元测试 — HP Scan Tool v4.0
覆盖: ADF 来源决策 / 单页与多页分支 / 缓存写入 / 取消透传 / 错误与取消信号
"""

import os
import threading
import time

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
    # 同时隔离配置文件路径：否则用户真实的 cache_config.json 里若配了
    # 自定义 cache_root，_cache_root() 会绕过 _default_cache_root 写到真实目录
    monkeypatch.setattr(scan_coordinator.cache_manager, "_cache_config_path",
                        lambda: os.path.join(root, "cache_config.json"))
    monkeypatch.setattr(scan_coordinator.cache_manager, "_config_cache", None)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_active_jobs", set())
    return root


@pytest.fixture
def qt_app():
    """Qt 事件循环（跨线程信号靠它投递到主线程）"""
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


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
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})):
            assert coordinator.resolve_source(scanner, "Auto") == ("Feeder", True)

    def test_platen_request_is_respected(self, coordinator, scanner):
        """用户显式选平板时，即使 ADF 有纸也不得被切成 Feeder，且不发起探测。

        改造前的 GUI `_detect_adf_source` 是"选平板就是平板"，这里必须保持一致，
        否则用户选了平板却被拿输稿器扫，是行为回归。
        """
        scanner.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})) as probe:
            assert coordinator.resolve_source(scanner, "Platen") == ("Platen", False)
            assert probe.call_count == 0

    def test_adf_empty_returns_platen(self, coordinator, scanner):
        scanner.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": False})):
            assert coordinator.resolve_source(scanner, "Feeder") == ("Platen", False)

    def test_probe_failure_keeps_requested_source(self, coordinator, scanner):
        """探测失败时必须保留用户请求的来源，不能静默改成平板。

        生产真实形态：probe_scanner_status 把异常吞掉、返回
        (False, {"state": "Unknown", "adf_loaded": False})，从不抛异常。
        """
        scanner.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(False, {"state": "Unknown", "adf_loaded": False})):
            assert coordinator.resolve_source(scanner, "Feeder") == ("Feeder", False)

    def test_probe_exception_keeps_requested_source(self, coordinator, scanner):
        """防御性：即便 probe_scanner_status 真的抛异常，来源也不得被改掉"""
        scanner.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        side_effect=RuntimeError("boom")):
            assert coordinator.resolve_source(scanner, "Feeder") == ("Feeder", False)

    def test_legit_unknown_state_is_probe_success(self, coordinator, scanner):
        """设备合法上报 <State>Unknown</State> 时，探测算成功，不该被当成探测失败"""
        scanner.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Unknown", "adf_loaded": False})):
            # Auto 模式下：探测成功但没纸 -> 落 Platen
            assert coordinator.resolve_source(scanner, "Auto") == ("Platen", False)
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Unknown", "adf_loaded": False})):
            # Feeder 模式下：探测成功但没纸 -> 落 Platen（不是保留 Feeder）
            assert coordinator.resolve_source(scanner, "Feeder") == ("Platen", False)

    @pytest.mark.parametrize("requested,probe_ok,adf_loaded,has_adf,expected", [
        ("Platen", True, True, True, ("Platen", False)),   # 显式选平板：保持平板
        ("Feeder", True, True, True, ("Feeder", True)),    # 选输稿器 + 有纸
        ("Feeder", True, False, True, ("Platen", False)),  # 选输稿器 + 无纸
        ("Feeder", False, False, True, ("Feeder", False)), # 选输稿器 + 探测失败：保留来源
        ("Auto", True, True, True, ("Feeder", True)),      # 自动 + 有纸
        ("Auto", True, False, True, ("Platen", False)),    # 自动 + 无纸
        ("Auto", False, False, True, ("Platen", False)),   # 自动 + 探测失败
        ("Feeder", True, True, False, ("Platen", False)),  # 设备无 ADF 能力
    ])
    def test_source_behavior_table(self, coordinator, scanner, requested,
                                   probe_ok, adf_loaded, has_adf, expected):
        """来源决策行为对照表：8 行必须同时成立。

        这是本次改造的验收契约——切到 probe_scanner_status 后，"探测失败"
        与"设备合法上报 Unknown"必须仍然分得开。
        """
        scanner.has_adf = has_adf
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(probe_ok,
                                      {"state": "Idle", "adf_loaded": adf_loaded})):
            assert coordinator.resolve_source(scanner, requested) == expected


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
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
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

    def test_multipage_empty_raises_cancelled(self, coordinator, scanner, tmp_path, cache_root):
        """Feeder 分支被取消（引擎返回空页）时也抛 ScanCancelled，与单页一致。

        否则 GUI 会收到"完成但 0 页"和"取消"两种信号。
        """
        scanner.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan", return_value=[]):
            with pytest.raises(ScanCancelled):
                coordinator.run_scan_sync(scanner, _params(tmp_path, source="ADF"))


class TestSignals:
    def test_failed_signal_emitted(self, coordinator, scanner, tmp_path, cache_root):
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


class TestStartScan:
    def test_start_scan_emits_finished(self, coordinator, scanner, tmp_path,
                                       cache_root, qt_app):
        """start_scan → 线程池 → 信号 finished，链路端到端可用"""
        received = []
        failed = []
        coordinator.scan_finished.connect(lambda jid, res: received.append((jid, res)))
        coordinator.scan_failed.connect(lambda jid, err: failed.append((jid, err)))
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(b"\xff\xd8fake", "jpg")):
            job_id = coordinator.start_scan(scanner, _params(tmp_path))
            assert job_id
            # 等线程池里的任务跑完。跨线程信号是队列投递到主线程的，
            # 必须转事件循环才会送达（GUI 侧本来就有事件循环）。
            deadline = time.time() + 5
            while time.time() < deadline and not received:
                qt_app.processEvents()
                time.sleep(0.02)
        assert not failed, f"扫描失败: {failed}"
        assert received, "start_scan 未发出 scan_finished"
        assert received[0][0] == job_id
        assert received[0][1].page_count == 1
        coordinator.shutdown()


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

    def test_start_scan_cancel_chain(self, coordinator, scanner, tmp_path,
                                     cache_root, qt_app):
        """端到端取消链路：start_scan → cancel_scan → 引擎中止 → scan_cancelled。

        补的是 I-3 的取消半边：现有 test_cancel_scan_sets_event 直写
        _cancel_events，绕过了"协调器自己注册 event"这一环，测不到
        cancel_scan 置位的是不是引擎拿到的那个 event。
        """
        cancelled = []
        finished = []
        failed = []
        observed = {}

        def fake_scan(**kwargs):
            # 模拟引擎在轮询点观察 cancel_event：置位才中止
            ev = kwargs.get("cancel_event")
            observed["event"] = ev
            deadline = time.time() + 5
            while time.time() < deadline and not (ev is not None and ev.is_set()):
                time.sleep(0.01)
            observed["engine_saw_cancel"] = bool(ev is not None and ev.is_set())
            return (None, "jpg")

        coordinator.scan_cancelled.connect(lambda jid: cancelled.append(jid))
        coordinator.scan_finished.connect(lambda jid, res: finished.append(jid))
        coordinator.scan_failed.connect(lambda jid, err: failed.append((jid, err)))

        with mock.patch("scan_coordinator.execute_scan", side_effect=fake_scan):
            job_id = coordinator.start_scan(scanner, _params(tmp_path))
            # 协调器自己注册了 job_id → cancel_event 的映射
            assert job_id in coordinator._cancel_events, "协调器未注册 cancel_event"
            registered = coordinator._cancel_events[job_id]

            # 等工作线程进入引擎（此时它正阻塞在 cancel_event 上）
            deadline = time.time() + 5
            while time.time() < deadline and "event" not in observed:
                qt_app.processEvents()
                time.sleep(0.01)
            assert "event" in observed, "工作线程未进入引擎"

            # 用注册表里的 job_id 取消
            coordinator.cancel_scan(job_id)

            # 等信号（跨线程信号靠主线程事件循环投递）
            deadline = time.time() + 5
            while time.time() < deadline and not cancelled:
                qt_app.processEvents()
                time.sleep(0.01)
        coordinator.shutdown()

        # 1) 引擎拿到的就是协调器注册的那个 event，cancel_scan 置位的也是它
        assert observed["event"] is registered
        assert registered.is_set()
        # 2) 引擎侧确实观察到了取消
        assert observed["engine_saw_cancel"] is True
        # 3) 收到 scan_cancelled，且没有 scan_finished / scan_failed
        assert cancelled == [job_id]
        assert finished == []
        assert failed == []
