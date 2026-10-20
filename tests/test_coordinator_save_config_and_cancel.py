r"""
协调器侧复审修复的守护测试 — HP Scan Tool v4.0

来源：`.superpowers/sdd/2026-10-20_扫描流程统一到协调器_实施计划_v1.0/verify-recheck-latest.md`
§3「新增的问题」，本文件只覆盖其中两条：

  · **A4-1**：协调器自己有 4 处调用点把 `save_config` 的返回值丢了
    （load_config / set_custom_name / 发现扫描仪后的持久化 / persist_scan_settings）
    ⇒ 写失败完全静默（用户以为改了设置，实际没落盘）。
    现在统一走 `_save_config_checked`：记 error 日志 + 置 `last_config_save_ok`
    + 发 `config_save_failed` 信号；能改返回值的两个方法直接返回 bool。

  · **A5 遗留隐患**：GUI 的 WIA 路径把自己的 cancel_event **直接写进协调器的
    私有字典**（`coordinator._cancel_events[job_id] = ev`）—— 功能是通的，
    但属于跨模块私有耦合。协调器侧新增**公开** API
    `register_external_cancel` / `unregister_external_cancel`，
    复用协调器自己的锁与同一张表（GUI 侧的切换方案见修复报告，本文件不碰 GUI）。

★ 用户数据隔离：tests/conftest.py 的 autouse fixture 已把 APPDATA 指向沙箱，
  本文件不读写真实 %APPDATA%\HP_Scan_Tool。
"""

import logging
import threading

import pytest

import scan_coordinator
from scan_coordinator import ScanCoordinator


@pytest.fixture
def coordinator():
    """协调器（cfg 置空，避免继承真实磁盘配置）"""
    c = ScanCoordinator()
    c.cfg = {}
    return c


@pytest.fixture
def failing_save(monkeypatch):
    """让配置写入一律失败（模拟拿不到跨进程锁 / 写盘异常）"""
    calls = []

    def _fail(cfg):
        calls.append(cfg)
        return False

    monkeypatch.setattr(scan_coordinator, "save_config", _fail)
    return calls


def _error_logs(caplog):
    return [r for r in caplog.records
            if r.levelno >= logging.ERROR and "配置未落盘" in r.getMessage()]


class TestConfigSaveReporting:
    """★ A4-1：协调器侧 4 处配置写入必须如实上报（不再是静默吞掉）"""

    def test_set_custom_name_returns_false_on_failure(self, coordinator, failing_save,
                                                       caplog):
        """set_custom_name：写失败 ⇒ 返回 False + error 日志 + 失败信号"""
        seen = []
        coordinator.config_save_failed.connect(seen.append)
        with caplog.at_level(logging.ERROR, logger="scan_coordinator"):
            ok = coordinator.set_custom_name("1.2.3.4", "打印间A")

        assert ok is False, "set_custom_name 必须如实返回写入结果"
        assert coordinator.last_config_save_ok is False
        assert seen, "写失败必须发 config_save_failed 信号（供 GUI 接线）"
        assert _error_logs(caplog), "写失败必须记 error 日志（不能只留 debug）"
        # 内存态照旧更新（与修复前一致：内存生效、落盘失败由上层感知）
        assert coordinator.cfg["nicknames"]["1.2.3.4"] == "打印间A"

    def test_set_custom_name_returns_true_on_success(self, coordinator):
        """写成功 ⇒ 返回 True（真实落盘，沙箱 APPDATA）"""
        ok = coordinator.set_custom_name("1.2.3.4", "打印间A")
        assert ok is True
        assert coordinator.last_config_save_ok is True

    def test_persist_scan_settings_returns_bool(self, coordinator, failing_save, caplog):
        """persist_scan_settings：写失败 ⇒ 返回 False + error 日志 + 失败信号"""
        seen = []
        coordinator.config_save_failed.connect(seen.append)
        with caplog.at_level(logging.ERROR, logger="scan_coordinator"):
            ok = coordinator.persist_scan_settings(600, "彩色", "PDF", "D:/out", "自动")

        assert ok is False
        assert coordinator.last_config_save_ok is False
        assert seen
        assert _error_logs(caplog)
        assert coordinator.cfg["resolution"] == 600     # 内存态照旧更新

    def test_persist_scan_settings_returns_true_on_success(self, coordinator):
        ok = coordinator.persist_scan_settings(600, "彩色", "PDF", "D:/out", "自动")
        assert ok is True
        assert coordinator.last_config_save_ok is True

    def test_load_config_keeps_dict_contract_and_reports_failure(self, coordinator,
                                                                 failing_save, caplog):
        """load_config：返回值契约不变（仍是 dict），失败改由标志 + 信号表达"""
        seen = []
        coordinator.config_save_failed.connect(seen.append)
        with caplog.at_level(logging.ERROR, logger="scan_coordinator"):
            cfg = coordinator.load_config()

        assert isinstance(cfg, dict), "load_config 必须仍返回 dict（GUI/老测试依赖）"
        assert cfg["deskew_enabled"] is True
        assert coordinator.last_config_save_ok is False
        assert seen, "补默认值的落盘失败必须发 config_save_failed 信号"
        assert _error_logs(caplog)

    def test_load_config_success_sets_flag_true(self, coordinator):
        cfg = coordinator.load_config()
        assert isinstance(cfg, dict)
        assert coordinator.last_config_save_ok is True

    def test_discover_background_reports_save_failure(self, coordinator, monkeypatch,
                                                       caplog):
        """发现扫描仪后台线程里的持久化：写失败必须被如实上报"""
        monkeypatch.setattr(scan_coordinator, "discover_all_scanners",
                            lambda timeout=3.0: [])
        monkeypatch.setattr(scan_coordinator, "save_config", lambda cfg: False)

        done = threading.Event()
        coordinator.discover_scanners_background(lambda n, s: done.set())
        assert done.wait(10), "发现线程未在超时内完成"
        # 回调在落盘之后触发 ⇒ 此时标志与日志都已更新
        assert coordinator.last_config_save_ok is False
        assert _error_logs(caplog)

    def test_failure_signal_carries_context(self, coordinator, failing_save):
        """失败信号带场景名（调用方可据此告诉用户是哪个设置没存上）"""
        seen = []
        coordinator.config_save_failed.connect(seen.append)
        coordinator.persist_scan_settings(600, "彩色", "PDF", "D:/out", "自动")
        assert seen and "扫描参数" in seen[0]


class TestExternalCancelApi:
    """★ 公开的外部取消登记 API（替代戳协调器私有字典）"""

    def test_register_then_cancel_scan_sets_event(self, coordinator):
        """登记后 cancel_scan(job_id) 能置位该 event"""
        ev = threading.Event()
        coordinator.register_external_cancel("job-ext", ev)
        assert coordinator._cancel_events["job-ext"] is ev, "必须进协调器同一张表"
        coordinator.cancel_scan("job-ext")
        assert ev.is_set()

    def test_unregister_stops_set(self, coordinator):
        """注销后不再置位（不残留）"""
        ev = threading.Event()
        coordinator.register_external_cancel("job-ext", ev)
        coordinator.unregister_external_cancel("job-ext")
        assert "job-ext" not in coordinator._cancel_events
        coordinator.cancel_scan("job-ext")
        assert not ev.is_set()

    def test_unregister_is_idempotent(self, coordinator):
        """注销没登记过的 job 不报错"""
        coordinator.unregister_external_cancel("never-registered")

    def test_cancel_only_affects_targeted_job(self, coordinator):
        """取消只影响目标 job（多任务并存时不误伤）"""
        ev_a, ev_b = threading.Event(), threading.Event()
        coordinator.register_external_cancel("A", ev_a)
        coordinator.register_external_cancel("B", ev_b)
        coordinator.cancel_scan("A")
        assert ev_a.is_set()
        assert not ev_b.is_set()

    def test_shutdown_sets_registered_events(self, coordinator):
        """shutdown 与内部登记的事件同等待遇（同一张表 ⇒ 同一把锁）"""
        ev = threading.Event()
        coordinator.register_external_cancel("job-ext", ev)
        coordinator.shutdown()
        assert ev.is_set()

    def test_rejects_invalid_arguments(self, coordinator):
        """非法参数当场报错（不静默接受）"""
        with pytest.raises(ValueError):
            coordinator.register_external_cancel("", threading.Event())
        with pytest.raises(ValueError):
            coordinator.register_external_cancel("j", None)
        with pytest.raises(TypeError):
            coordinator.register_external_cancel("j", object())

    def test_external_and_internal_share_one_table(self, coordinator):
        """外部登记与 start_scan 内部登记互不干扰（同一张表、不同 key）"""
        ev = threading.Event()
        coordinator.register_external_cancel("job-ext", ev)
        coordinator._cancel_events["job-internal"] = threading.Event()
        coordinator.cancel_scan("job-ext")
        assert ev.is_set()
        assert not coordinator._cancel_events["job-internal"].is_set()
