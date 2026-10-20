"""
取消异常的跨层契约 — 回归护栏

背景（2026-10-21）：
  WIA 路径接入可取消能力后，引擎侧抛的是 `escl_engine.ScanCancelled`；
  而协调器当时自己定义了一个**互不相关**的 `class ScanCancelled(Exception)`。
  ⇒ 引擎抛的取消越过 `except ScanCancelled` 直落 `except Exception`
  ⇒ **用户点取消却被报成"扫描失败"**（发 `scan_failed` 而不是 `scan_cancelled`）。

  修法：协调器的 `ScanCancelled` 改为**继承**引擎那个类，
  并且捕获点捕获**基类**（`except EngineScanCancelled`）——
  只捕获子类会漏掉引擎抛的那一半。

本文件锁死这条契约：
  ① 协调器的 `ScanCancelled` 是 `escl_engine.ScanCancelled` 的子类
  ② 引擎抛取消 ⇒ 发 `scan_cancelled`，**不发** `scan_failed`
  ③ 引擎抛取消的子类（`ScanJobCancelled`）也一样
  ④ 协调器自己抛的取消也一样
  ⑤ 真正的失败仍然发 `scan_failed`（别把取消和失败混为一谈）
"""

import os
import time
from unittest import mock

import pytest

import escl_engine
import scan_coordinator
from scan_coordinator import ScanCancelled, ScanCoordinator


@pytest.fixture
def coordinator():
    c = ScanCoordinator()
    c.cfg = {}
    return c


@pytest.fixture
def scanner():
    s = escl_engine.ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    s.has_adf = False
    return s


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    root = str(tmp_path / "cache")
    os.makedirs(root, exist_ok=True)
    cm = scan_coordinator.cache_manager
    monkeypatch.setattr(cm, "_default_cache_root", lambda: root)
    monkeypatch.setattr(cm, "_cache_config_path",
                        lambda: os.path.join(root, "cache_config.json"))
    monkeypatch.setattr(cm, "_config_cache", None)
    monkeypatch.setattr(cm, "_config_cache_time", 0)
    monkeypatch.setattr(cm, "_active_jobs", set())
    monkeypatch.setattr(cm, "_pending_jobs", set())
    monkeypatch.setattr(cm, "_cache_size_cache", None)
    return root


def _params(tmp_path):
    return {
        "resolution": 300,
        "color_mode_ui": "彩色",
        "output_format": "jpg",
        "output_dir": str(tmp_path),
        "source": "Platen",
    }


def _run_and_collect(coordinator, scanner, tmp_path, qt_app, side_effect):
    """跑一次扫描，收集三个信号的结果。返回 (cancelled, finished, failed)。"""
    cancelled, finished, failed = [], [], []
    coordinator.scan_cancelled.connect(lambda jid: cancelled.append(jid))
    coordinator.scan_finished.connect(lambda jid, res: finished.append(jid))
    coordinator.scan_failed.connect(lambda jid, err: failed.append((jid, err)))

    with mock.patch("scan_coordinator.execute_scan", side_effect=side_effect):
        job_id = coordinator.start_scan(scanner, _params(tmp_path))
        deadline = time.time() + 5
        while time.time() < deadline and not (cancelled or finished or failed):
            qt_app.processEvents()
            time.sleep(0.01)
        # 再泵一会儿，确保没有迟到的信号
        for _ in range(20):
            qt_app.processEvents()
            time.sleep(0.005)
    return cancelled, finished, failed


class TestCancelExceptionContract:
    def test_coordinator_cancelled_is_subclass_of_engine_cancelled(self):
        """① 协调器的取消异常必须是引擎那个类的子类"""
        assert issubclass(ScanCancelled, escl_engine.ScanCancelled)
        assert issubclass(ScanCancelled, Exception)

    def test_engine_raised_cancel_emits_scan_cancelled(self, coordinator, scanner,
                                                        tmp_path, cache_root, qt_app):
        """② 引擎抛 `escl_engine.ScanCancelled` ⇒ 只发 scan_cancelled"""
        def boom(**kwargs):
            raise escl_engine.ScanCancelled("WIA 扫描已取消")

        cancelled, finished, failed = _run_and_collect(
            coordinator, scanner, tmp_path, qt_app, boom)

        assert len(cancelled) == 1, f"应发 scan_cancelled，实际 cancelled={cancelled}"
        assert failed == [], f"取消不得被报成失败：failed={failed}"
        assert finished == [], f"取消不得同时发 scan_finished：finished={finished}"

    def test_engine_subclass_cancel_also_emits_scan_cancelled(self, coordinator, scanner,
                                                               tmp_path, cache_root, qt_app):
        """③ 引擎抛取消异常的子类（ScanJobCancelled）也一样"""
        def boom(**kwargs):
            raise escl_engine.ScanJobCancelled("建 job 期间被取消")

        cancelled, finished, failed = _run_and_collect(
            coordinator, scanner, tmp_path, qt_app, boom)

        assert len(cancelled) == 1, f"应发 scan_cancelled，实际 cancelled={cancelled}"
        assert failed == [], f"取消不得被报成失败：failed={failed}"

    def test_coordinator_own_cancel_still_emits_scan_cancelled(self, coordinator, scanner,
                                                                tmp_path, cache_root, qt_app):
        """④ 协调器自己抛的取消仍然走 scan_cancelled（既有行为不回归）"""
        def boom(**kwargs):
            raise ScanCancelled("job-1")

        cancelled, finished, failed = _run_and_collect(
            coordinator, scanner, tmp_path, qt_app, boom)

        assert len(cancelled) == 1, f"应发 scan_cancelled，实际 cancelled={cancelled}"
        assert failed == [], f"取消不得被报成失败：failed={failed}"

    def test_real_failure_still_emits_scan_failed(self, coordinator, scanner,
                                                  tmp_path, cache_root, qt_app):
        """⑤ 真正的失败仍然发 scan_failed（别把取消和失败混为一谈）"""
        def boom(**kwargs):
            raise RuntimeError("设备不可达")

        cancelled, finished, failed = _run_and_collect(
            coordinator, scanner, tmp_path, qt_app, boom)

        assert len(failed) == 1, f"应发 scan_failed，实际 failed={failed}"
        assert cancelled == [], f"失败不得被误报成取消：cancelled={cancelled}"
