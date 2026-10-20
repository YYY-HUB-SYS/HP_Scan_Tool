"""
协调器生命周期护栏 — HP Scan Tool v4.0（终审 Important I-1 / I-3）

被守护的行为
------------
**I-1（取消竞态 ⇒ 成功被改判成"已取消" + job 永不回收）**

扫描**已经成功取到页**之后，不得再被"取消"改判成 `scan_cancelled`：
一次**完全成功**的扫描被报成"已取消"⇒ GUI 不弹预览、不给保存入口
⇒ 用户已经扫出来（纸上已经印过）的页拿不到。

判定条件（被守护的那条线）：
  - 取消发生在**取页之前 / 之中** ⇒ "取消优先"语义原样保留
    （发 `scan_cancelled`，不交付结果，job 完全注销）；
  - 取消发生在**成功之后**（页已落盘、元数据已写）⇒ 不改判，
    扫描按**成功**交付（发 `scan_finished`）。

无论走哪条分支，job 都不能停在"**受保护且无人认领**"的状态：
受保护的 job 会被 `cache_manager._evict_oldest_jobs_locked` 与
`cleanup_stale` **双双跳过** ⇒ 缓存目录永远占着磁盘（终审 F-1 实测：
`protected: {'jobR'}`）。

**I-3（`page_meta.set_page()` 返回 False 被丢弃 ⇒ 页元数据整批静默丢失）**

`set_page()` 在"拿不到跨进程锁 / 原文件已损坏"时返回 False（**不抛异常**），
调用方必须接住：
  - 记 **error** 日志（不是 debug —— 默认日志级别下要看得见）；
  - 发**用户可见信号**（`page_meta_save_failed`）；
  - **不得**因此中断整批扫描（用户更希望"页扫到了、元数据没写上"并被告知）。
"""

import logging
import os
import threading
import time

import pytest
from unittest import mock

import cache_manager
import page_meta
import scan_coordinator
from scan_coordinator import ScanCoordinator


# ── fixtures ──────────────────────────────────────────────


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
    """隔离缓存根（页文件 + page_meta.json 都落在 tmp_path 下）"""
    root = str(tmp_path / "cache")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: root)
    # 同时隔离缓存配置：否则真实 cache_config.json 里若配了自定义 cache_root，
    # _cache_root() 会绕过 _default_cache_root 写到真实目录
    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: os.path.join(root, "cache_config.json"))
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(cache_manager, "_active_jobs", set())
    monkeypatch.setattr(cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(cache_manager, "_cache_size_cache", None)
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


def _jpeg_bytes():
    """一张最小可解析的 JPEG（让 _estimate_page_skew / 尺寸读取有真数据）"""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="JPEG")
    return buf.getvalue()


def _run_task(coord, scanner, params, job_id, cancel_event):
    """在当前线程跑一次 _ScanTask（等价于线程池里跑，但确定性更好）"""
    task = scan_coordinator._ScanTask(coord, scanner, params, job_id, cancel_event)
    task.run()
    return task


# ── I-1：取消竞态 ─────────────────────────────────────────


class TestI1CancelRace:
    """扫描成功后不得被"取消"改判；取消优先语义必须原样保留。"""

    def test_cancel_set_at_completion_boundary_reports_success(self, coordinator,
                                                                scanner, tmp_path,
                                                                cache_root):
        """★ 终审 F-1 的最小复现：取消标志在 `run_scan_sync` 返回前一刻被置位。

        复现点与 reviewer 实测**同一处**：`cache_manager.register_pending_job`
        （`run_scan_sync` 的最后一步）—— 在它返回后立刻置位取消事件。

        期望：这一次扫描是**成功**的 ⇒ 必须发 `scan_finished`，
        绝不能发 `scan_cancelled`（否则用户扫成功的页拿不到）。
        """
        coord = coordinator
        finished, cancelled, failed = [], [], []
        coord.scan_finished.connect(lambda jid, res: finished.append(jid))
        coord.scan_cancelled.connect(lambda jid: cancelled.append(jid))
        coord.scan_failed.connect(lambda jid, err: failed.append((jid, err)))

        ev = threading.Event()
        job_id = "job_race"
        real_register = cache_manager.register_pending_job

        def register_and_cancel(jid):
            real_register(jid)
            ev.set()          # ← 取消在"扫描已成功"之后到达

        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(_jpeg_bytes(), "jpg")), \
             mock.patch("scan_coordinator.cache_manager.register_pending_job",
                        side_effect=register_and_cancel):
            _run_task(coord, scanner, _params(tmp_path), job_id, ev)

        assert failed == [], "这次扫描不该失败"
        assert cancelled == [], (
            "扫描**已经成功**，却被改判成『已取消』—— 用户扫成功的页拿不到")
        assert finished == [job_id], "必须按成功交付结果"

    def test_cancel_before_page_acquisition_still_wins(self, coordinator,
                                                       scanner, tmp_path,
                                                       cache_root):
        """取消发生在**取页之中** ⇒ "取消优先"语义必须原样保留。

        （引擎返回数据的同时置位取消事件 ⇒ 协调器的取页后检查必须拦下它。）
        """
        coord = coordinator
        finished, cancelled = [], []
        coord.scan_finished.connect(lambda jid, res: finished.append(jid))
        coord.scan_cancelled.connect(lambda jid: cancelled.append(jid))

        ev = threading.Event()

        def fake_scan(**kwargs):
            ev.set()          # ← 取消在取页期间到达
            return (_jpeg_bytes(), "jpg")

        with mock.patch("scan_coordinator.execute_scan", side_effect=fake_scan):
            _run_task(coord, scanner, _params(tmp_path), "job_mid", ev)

        assert cancelled == ["job_mid"], "取页期间的取消必须仍然报『已取消』"
        assert finished == [], "取页期间的取消不得交付结果"

    def test_cancelled_job_is_never_left_protected(self, coordinator, scanner,
                                                   tmp_path, cache_root):
        """取消之后 job 必须**完全注销**（既不在 _active_jobs 也不在 _pending_jobs）。

        受保护的 job 会被 eviction / stale 清理双双跳过 ⇒ 缓存目录永不回收
        （终审 F-1 实测：`protected: {'jobR'}` + 缓存目录残留）。
        """
        coord = coordinator
        ev = threading.Event()

        def fake_scan(**kwargs):
            ev.set()
            return (_jpeg_bytes(), "jpg")

        job_id = "job_leak"
        with mock.patch("scan_coordinator.execute_scan", side_effect=fake_scan):
            _run_task(coord, scanner, _params(tmp_path), job_id, ev)

        assert job_id not in cache_manager._active_jobs, "取消后不得留在 _active_jobs"
        assert job_id not in cache_manager._pending_jobs, "取消后不得留在 _pending_jobs"
        assert not cache_manager.is_job_protected(job_id), (
            "取消后 job 仍受保护 ⇒ eviction / stale 清理都跳过它 ⇒ 永不回收")

    def test_cancelled_job_page_meta_is_discarded(self, coordinator, scanner,
                                                   tmp_path, cache_root):
        """取消 ⇒ 页元数据一并丢弃（既有语义：失败/取消路径 `_discard_page_meta`）。"""
        coord = coordinator
        ev = threading.Event()

        def fake_scan(**kwargs):
            ev.set()
            return (_jpeg_bytes(), "jpg")

        job_id = "job_meta_cancel"
        with mock.patch("scan_coordinator.execute_scan", side_effect=fake_scan):
            _run_task(coord, scanner, _params(tmp_path), job_id, ev)

        store = page_meta.PageMetaStore(cache_root)
        assert store.get_job(job_id) == {}, "取消后不得残留页元数据"

    def test_successful_scan_keeps_job_protected_for_preview(self, coordinator,
                                                              scanner, tmp_path,
                                                              cache_root):
        """成功路径：job 进入"已扫描未保存"保护态，等 GUI 预览生命周期释放。

        ★ 这不是泄漏：GUI 侧 `register_pending_job`（hp_scan_gui_pyside.py:2649）
          与 `PreviewDialog._release_job` → `_release_job_protection`
          （:429 unregister_job + unregister_pending_job）成对出现，
          用户保存（save_manager.remove_job）或关闭预览都会释放它。
        ⇒ 守护这条不变量，避免有人为了"不残留"把成功路径的保护也删掉
          （那会让用户预览期间缓存被 eviction 删掉 ⇒ 产物少页）。
        """
        coord = coordinator
        job_id = "job_ok"
        ev = threading.Event()
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(_jpeg_bytes(), "jpg")):
            _run_task(coord, scanner, _params(tmp_path), job_id, ev)

        assert job_id in cache_manager._pending_jobs, (
            "成功路径必须把 job 留在『已扫描未保存』保护态（等 GUI 预览释放）")
        assert cache_manager.is_job_protected(job_id)

    def test_shutdown_does_not_deliver_result(self, coordinator, scanner,
                                              tmp_path, cache_root):
        """★ 关窗（`shutdown()`）期间完成的扫描**不得**交付结果。

        关窗时 GUI 会 `coordinator.shutdown()`（hp_scan_gui_pyside.py:3907），
        它置位**全部**取消事件。若此时还发 `scan_finished`，GUI 会在关窗
        过程中弹预览对话框（主窗口已在销毁中）⇒ 卡死 / RuntimeError。

        ⇒ 关窗 ⇒ 报"已取消" + 完全注销 job（不残留、不弹窗）。
        """
        coord = coordinator
        finished, cancelled = [], []
        coord.scan_finished.connect(lambda jid, res: finished.append(jid))
        coord.scan_cancelled.connect(lambda jid: cancelled.append(jid))

        coord.shutdown()          # 先关窗：置位全部取消事件 + 标记正在关闭

        job_id = "job_shutdown"
        ev = threading.Event()
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(_jpeg_bytes(), "jpg")):
            _run_task(coord, scanner, _params(tmp_path), job_id, ev)

        assert finished == [], "关窗过程中不得交付结果（会弹预览框）"
        assert cancelled == [job_id], "关窗期间的扫描应报『已取消』"
        assert not cache_manager.is_job_protected(job_id), (
            "关窗取消后 job 不得留在受保护集合里（残留 ⇒ 永不回收）")


# ── I-3：页元数据写入失败必须可见 ───────────────────────────


class TestI3PageMetaWriteFailure:
    """`set_page()` 返回 False 必须被接住（error 日志 + 可见信号）。"""

    @pytest.fixture
    def failures(self, coordinator):
        got = []
        sig = getattr(coordinator, "page_meta_save_failed", None)
        assert sig is not None, (
            "协调器必须有 page_meta_save_failed 信号"
            "（页元数据写入失败必须对用户可见）")
        sig.connect(lambda jid, page_no: got.append((jid, page_no)))
        return got

    def test_set_page_false_is_reported_not_swallowed(self, coordinator, scanner,
                                                      tmp_path, cache_root,
                                                      failures, caplog):
        """★ 终审 F-3：`set_page` 返回 False（拿不到锁 / 原文件损坏）被丢弃
        ⇒ 用户看到"扫描成功"，产物却既没有自动纠偏、也没有手动旋转。

        期望：① error 日志 ② `page_meta_save_failed` 信号 ③ 扫描**不中断**。
        """
        coord = coordinator
        finished, cancelled = [], []
        coord.scan_finished.connect(lambda jid, res: finished.append(jid))
        coord.scan_cancelled.connect(lambda jid: cancelled.append(jid))

        job_id = "job_meta_fail"
        ev = threading.Event()
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(_jpeg_bytes(), "jpg")), \
             mock.patch.object(page_meta.PageMetaStore, "set_page",
                               return_value=False):
            with caplog.at_level(logging.ERROR, logger="scan_coordinator"):
                _run_task(coord, scanner, _params(tmp_path), job_id, ev)

        assert failures == [(job_id, 1)], (
            "页元数据写入失败必须发信号上报（否则等于静默丢弃）")
        errors = [r for r in caplog.records
                  if r.levelno >= logging.ERROR and r.name == "scan_coordinator"]
        assert errors, "页元数据写入失败必须记 **error** 日志（debug 看不见）"

        # 不中断整批扫描：用户更希望"页扫到了、元数据没写上"并被告知
        assert finished == [job_id], "一次写失败不得中断整批扫描"
        assert cancelled == []

    def test_failure_counter_is_exposed(self, coordinator, scanner, tmp_path,
                                        cache_root, failures):
        """协调器上要能查到本批失败了多少页（GUI 提示要用的数字）。"""
        coord = coordinator
        job_id = "job_meta_count"
        ev = threading.Event()
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(_jpeg_bytes(), "jpg")), \
             mock.patch.object(page_meta.PageMetaStore, "set_page",
                               return_value=False):
            _run_task(coord, scanner, _params(tmp_path), job_id, ev)

        assert coord.page_meta_failed_pages >= 1, (
            "协调器必须暴露失败页数（GUI 提示需要）")

    def test_no_signal_when_page_meta_written_ok(self, coordinator, scanner,
                                                 tmp_path, cache_root, failures):
        """正常路径**不得**误报（否则用户会看到"元数据没写上"的假警告）。"""
        coord = coordinator
        job_id = "job_meta_ok"
        ev = threading.Event()
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(_jpeg_bytes(), "jpg")):
            _run_task(coord, scanner, _params(tmp_path), job_id, ev)

        assert failures == [], "写入成功时不得发失败信号"
        assert coord.page_meta_failed_pages == 0

        store = page_meta.PageMetaStore(cache_root)
        assert store.get_job(job_id), "正常路径必须真的写进页元数据"


# ── I-3 的"不得中断整批扫描"补充：多页里部分页失败 ───────────


class TestI3PartialFailure:
    def test_multipage_partial_meta_failure_does_not_abort_batch(self, coordinator,
                                                                  scanner,
                                                                  tmp_path,
                                                                  cache_root):
        """多页批次里某一页元数据写失败 ⇒ 其余页照常交付，整批不中断。"""
        coord = coordinator
        finished, failures = [], []
        coord.scan_finished.connect(lambda jid, res: finished.append(jid))
        coord.page_meta_save_failed.connect(
            lambda jid, page_no: failures.append((jid, page_no)))

        scanner.has_adf = True
        pages = [(_jpeg_bytes(), "jpg"), (_jpeg_bytes(), "jpg"),
                 (_jpeg_bytes(), "jpg")]
        real_set_page = page_meta.PageMetaStore.set_page
        calls = {"n": 0}

        def flaky_set_page(self, job_id, page_no, meta):
            calls["n"] += 1
            if calls["n"] == 2:          # 第 2 页写失败
                return False
            return real_set_page(self, job_id, page_no, meta)

        job_id = "job_partial"
        ev = threading.Event()
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan", return_value=pages), \
             mock.patch.object(page_meta.PageMetaStore, "set_page", flaky_set_page):
            _run_task(coord, scanner, _params(tmp_path, source="ADF"),
                      job_id, ev)

        assert finished == [job_id], "部分页元数据失败不得中断整批扫描"
        assert failures == [(job_id, 2)], "失败页必须被上报"
