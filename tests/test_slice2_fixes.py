"""
轮 1 · 片 2 修复波次的回归测试 — HP Scan Tool v4.0

覆盖本波次修掉的 9 条 finding：
  F-2 scan_config.json 收口到 config_io（并发写不再产出非法 JSON）
  F-3 resolve_source 区分"设备没有 ADF"与"我们没查到能力"
  F-4 预览期 job 受 eviction 保护 + 失败页可被调用方取到
  F-5 PDF 体积预检 + 后台保存入口 + docstring 不再说谎
  F-7 history 读失败（OSError）不覆盖原文件 + 备份
  E-1 _a4_canvas 永不裁切（600 DPI 不再丢 75% 内容）
  E-2 最终 PDF 原子写（失败不留半截文件）
  E-3 write_page 不再每页全量遍历缓存根
  E-4 profile_manager 缓存 TTL（跨进程写入能被读到）
"""

import json
import os
import threading
import time
from unittest import mock

import pytest
from PIL import Image
from pypdf import PdfReader

import cache_manager
import history_manager
import profile_manager
import save_manager
import scan_coordinator
from scan_coordinator import ScanCoordinator


# ── 夹具 ──────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """缓存根 / 历史 / 设备配置 全部重定向到临时目录（不碰真实 %APPDATA%）"""
    cache_root = str(tmp_path / "cache")
    os.makedirs(cache_root, exist_ok=True)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()

    monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: cache_root)
    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: os.path.join(cache_root, "cache_config.json"))
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(cache_manager, "_active_jobs", set())
    monkeypatch.setattr(cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(cache_manager, "_cache_size_cache", None)
    monkeypatch.setattr(history_manager, "_history_file",
                        lambda: str(cfg_dir / "history.json"))
    monkeypatch.setattr(history_manager, "HISTORY_FILE", None)
    monkeypatch.setattr(profile_manager, "_profile_path",
                        lambda: str(cfg_dir / "profiles.json"))
    monkeypatch.setattr(profile_manager, "_profiles_cache", None)
    monkeypatch.setattr(profile_manager, "_profiles_cache_time", 0)
    return {"cache_root": cache_root, "cfg_dir": str(cfg_dir)}


@pytest.fixture
def jpg(tmp_path):
    p = tmp_path / "src.jpg"
    Image.new("RGB", (100, 200), "white").save(p, "JPEG")
    return str(p)


@pytest.fixture
def scanner():
    from escl_engine import ScannerInfo
    s = ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    s.has_adf = False
    return s


@pytest.fixture
def coordinator():
    c = ScanCoordinator()
    c.cfg = {}
    return c


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


def _tiny_cache_config(cache_root):
    """把缓存上限压到 1KB（触发线 512B / 清理 50B）"""
    cfg_path = os.path.join(cache_root, "cache_config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "cache_limit_mb": 1024 / (1024 * 1024),
            "trigger_ratio": 0.5,
            "cleanup_mb": 50 / (1024 * 1024),
            "stale_hours": 24,
        }, f)
    cache_manager._config_cache = None
    cache_manager._config_cache_time = 0


# ── F-2：scan_config.json 收口到 config_io ─────────────────


class TestF2ScanConfigAtomic:
    def test_save_config_writes_valid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scan_coordinator, "_app_dir", lambda: str(tmp_path))
        scan_coordinator.save_config({"resolution": 600, "output_dir": "D:/x"})
        path = tmp_path / "scan_config.json"
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "resolution": 600, "output_dir": "D:/x"}

    def test_no_fixed_tmp_file_left_behind(self, tmp_path, monkeypatch):
        """旧的固定临时名 scan_config.json.tmp 不得再出现（并发写会交错）。"""
        monkeypatch.setattr(scan_coordinator, "_app_dir", lambda: str(tmp_path))
        scan_coordinator.save_config({"a": 1})
        assert not (tmp_path / "scan_config.json.tmp").exists()

    def test_concurrent_saves_always_valid_json(self, tmp_path, monkeypatch):
        """并发写 ⇒ 文件始终是合法 JSON，且不丢键（旧实现会交错写坏 JSON）。"""
        monkeypatch.setattr(scan_coordinator, "_app_dir", lambda: str(tmp_path))
        errors = []
        path = tmp_path / "scan_config.json"

        def _writer(idx):
            try:
                for _ in range(15):
                    scan_coordinator.save_config(
                        {"writer": idx, "resolution": 300 + idx, "filler": "x" * 64})
                    # 写完立刻回读：任何时刻都必须是合法 JSON
                    json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:  # pragma: no cover - 失败即视为缺陷
                errors.append(e)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        final = json.loads(path.read_text(encoding="utf-8"))
        assert "writer" in final and "resolution" in final


# ── F-3：能力未知 vs 设备真的没有 ADF ─────────────────────


class TestF3CapabilityKnown:
    def test_tracked_fetch_marks_known_when_fields_change(self):
        from escl_engine import ScannerInfo
        s = ScannerInfo(name="t", ip="1.2.3.4")
        assert not scan_coordinator.capabilities_known(s)

        def _fake_fetch(scanner, timeout=3.0):
            scanner.model = "HP M232"
            return scanner

        with mock.patch("scan_coordinator.fetch_capabilities", _fake_fetch):
            scan_coordinator.fetch_capabilities_tracked(s, timeout=1.0)
        assert scan_coordinator.capabilities_known(s)

    def test_tracked_fetch_leaves_unknown_when_query_fails(self):
        """能力查询失败（字段原封不动）⇒ 判定为"能力未知"，不是"没有 ADF"。"""
        from escl_engine import ScannerInfo
        s = ScannerInfo(name="t", ip="1.2.3.4")

        with mock.patch("scan_coordinator.fetch_capabilities",
                        lambda scanner, timeout=3.0: scanner):
            scan_coordinator.fetch_capabilities_tracked(s, timeout=1.0)
        assert not scan_coordinator.capabilities_known(s)

    def test_tracked_fetch_survives_exception(self):
        from escl_engine import ScannerInfo
        s = ScannerInfo(name="t", ip="1.2.3.4")

        def _boom(scanner, timeout=3.0):
            raise RuntimeError("network down")

        with mock.patch("scan_coordinator.fetch_capabilities", _boom):
            scan_coordinator.fetch_capabilities_tracked(s, timeout=1.0)
        assert not scan_coordinator.capabilities_known(s)

    def test_feeder_kept_when_capability_unknown(self, coordinator, scanner):
        """F-3 核心：能力未知 + 用户选输稿器 ⇒ 仍是 Feeder，且有提示。"""
        source, adf, warning = coordinator.resolve_source_detailed(scanner, "Feeder")
        assert (source, adf) == ("Feeder", False)
        assert warning == scan_coordinator.WARN_CAPABILITY_UNKNOWN

    def test_platen_forced_when_capability_known_without_adf(self, coordinator, scanner):
        scan_coordinator.mark_capabilities_known(scanner)
        source, adf, warning = coordinator.resolve_source_detailed(scanner, "Feeder")
        assert (source, adf) == ("Platen", False)
        assert warning == scan_coordinator.WARN_NO_ADF

    def test_platen_request_never_probes(self, coordinator, scanner):
        with mock.patch("scan_coordinator.probe_scanner_status") as probe:
            source, adf, warning = coordinator.resolve_source_detailed(scanner, "Platen")
        assert (source, adf) == ("Platen", False)
        assert warning is None
        assert probe.call_count == 0

    def test_auto_is_decided_by_probe(self, coordinator, scanner):
        scanner.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})):
            assert coordinator.resolve_source(scanner, "Auto") == ("Feeder", True)
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": False})):
            assert coordinator.resolve_source(scanner, "Auto") == ("Platen", False)

    def test_source_warning_signal_emitted(self, coordinator, scanner, sandbox,
                                            tmp_path):
        """能力未确认时必须有一条**用户可见**的提示（GUI 侧接线用）。"""
        received = []
        coordinator.source_warning.connect(lambda msg: received.append(msg))
        with mock.patch("scan_coordinator.execute_multipage_scan",
                        return_value=[(b"\xff\xd8fake", "jpg")]):
            coordinator.run_scan_sync(scanner, _params(tmp_path, source="ADF"))
        assert received, "能力未确认时必须发 source_warning 信号"
        assert scan_coordinator.WARN_CAPABILITY_UNKNOWN in received[0]


# ── F-4：预览期受保护 + 失败页可取回 ───────────────────────


class TestF4PendingJobProtection:
    def test_pending_job_survives_eviction(self, sandbox):
        """扫描完成未保存时，即使缓存越过触发线，该 job 目录也不能被删。"""
        cache_root = sandbox["cache_root"]
        _tiny_cache_config(cache_root)

        # 一个更旧的普通 job（应当被 FIFO 清理）
        cache_manager.write_page("job_old", 1, b"o" * 500, "jpg")
        time.sleep(0.05)

        # 一个"已扫描未保存"的 job（必须活下来）
        cache_manager.register_job("job_pending")
        cache_manager.write_page("job_pending", 1, b"p" * 300, "jpg")
        cache_manager.register_pending_job("job_pending")

        # 再写一页把缓存顶过触发线 ⇒ 触发 eviction
        cache_manager.write_page("job_new", 1, b"n" * 300, "jpg")

        assert os.path.exists(os.path.join(cache_root, "job_pending")), \
            "预览期的 job 被 eviction 删掉了（用户会丢页）"
        assert not os.path.exists(os.path.join(cache_root, "job_old")), \
            "普通旧 job 应当被 FIFO 清理"

    def test_save_releases_protection_and_removes_dir(self, sandbox, jpg, tmp_path):
        job_id = "job_save"
        cache_manager.register_job(job_id)
        with open(jpg, "rb") as f:
            data = f.read()
        cache_path = cache_manager.write_page(job_id, 1, data, "jpg")
        cache_manager.register_pending_job(job_id)
        assert cache_manager.is_job_protected(job_id)

        save_manager.save_pages(job_id, "jpg", str(tmp_path / "out.jpg"),
                                [(cache_path, 0)], [[0]])

        assert not os.path.isdir(os.path.join(sandbox["cache_root"], job_id))
        assert not cache_manager.is_job_protected(job_id)

    def test_failed_scan_leaves_no_protected_entry(self, coordinator, scanner,
                                                   sandbox, tmp_path):
        with mock.patch("scan_coordinator.execute_scan",
                        side_effect=RuntimeError("扫描超时")):
            with pytest.raises(RuntimeError):
                coordinator.run_scan_sync(scanner, _params(tmp_path), job_id="job_fail")
        assert "job_fail" not in cache_manager.protected_job_ids()

    def test_cancelled_scan_leaves_no_protected_entry(self, coordinator, scanner,
                                                      sandbox, tmp_path):
        with mock.patch("scan_coordinator.execute_scan", return_value=(None, "jpg")):
            with pytest.raises(scan_coordinator.ScanCancelled):
                coordinator.run_scan_sync(scanner, _params(tmp_path), job_id="job_cancel")
        assert "job_cancel" not in cache_manager.protected_job_ids()

    def test_successful_scan_keeps_job_protected(self, coordinator, scanner,
                                                  sandbox, tmp_path):
        """扫描成功但还没保存 ⇒ 仍受保护（这正是 F-4 的核心不变量）。"""
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(b"\xff\xd8fake", "jpg")):
            coordinator.run_scan_sync(scanner, _params(tmp_path), job_id="job_ok")
        assert "job_ok" in cache_manager.protected_job_ids()

    def test_unregister_pending_job_releases_without_deleting(self, sandbox):
        cache_manager.register_job("job_discard")
        cache_manager.write_page("job_discard", 1, b"x" * 10, "jpg")
        cache_manager.register_pending_job("job_discard")
        cache_manager.unregister_pending_job("job_discard")
        assert not cache_manager.is_job_protected("job_discard")
        assert os.path.isdir(os.path.join(sandbox["cache_root"], "job_discard"))

    def test_cleanup_stale_skips_protected_job(self, sandbox):
        d = os.path.join(sandbox["cache_root"], "job_old_pending")
        os.makedirs(d)
        with open(os.path.join(d, "page_001.jpg"), "wb") as f:
            f.write(b"old")
        old = time.time() - 25 * 3600
        os.utime(d, (old, old))
        cache_manager.register_pending_job("job_old_pending")

        cache_manager.cleanup_stale()

        assert os.path.exists(d), "受保护的 job 不该被 stale 清理删掉"

    def test_failed_pages_are_visible_to_caller(self, sandbox, tmp_path, jpg):
        """某页缺失时，失败数必须能被调用方取到（不是只累加局部计数）。"""
        missing = str(tmp_path / "does_not_exist.jpg")
        result = save_manager.save_pages(
            "job_fail_page", "jpg", str(tmp_path / "out.jpg"),
            [(jpg, 0), (missing, 0)], [[0, 1]])

        assert isinstance(result, list)          # 仍是 list（现有调用方不受影响）
        assert len(result) == 1                  # 成功落盘 1 个文件
        assert result.failed_pages == 1
        assert result.skipped_pages == [1]
        assert result.saved_page_count == 1

    def test_history_page_count_matches_actual_pages(self, sandbox, tmp_path, jpg):
        """历史里的页数必须是**实际写出的页数**，不能是请求的页数。"""
        missing = str(tmp_path / "missing.jpg")
        save_manager.save_pages(
            "job_hist", "jpg", str(tmp_path / "out.jpg"),
            [(jpg, 0), (missing, 0)], [[0, 1]])

        records = history_manager.list_records(limit=10)
        assert records, "保存后必须写历史"
        assert records[0]["page_count"] == 1


# ── F-5：体积预检 + 后台入口 + docstring ───────────────────


class TestF5VolumeGuard:
    def test_module_docstring_does_not_claim_constant_memory(self):
        """docstring 不得再说"内存不随页数线性增长"（那是假的）。"""
        doc = save_manager.__doc__ or ""
        assert "内存不随页数线性增长" not in doc
        assert "线性增长" in doc, "docstring 必须如实说明阶段二随页数线性增长"

    def test_estimate_uses_real_cache_page_sizes(self, sandbox, jpg):
        specs = [(jpg, 0), (jpg, 0)]
        est = save_manager.estimate_pdf_peak_bytes(specs)
        expected = int(os.path.getsize(jpg) * 2 * save_manager.PDF_PEAK_FACTOR)
        assert est == expected

    def test_estimate_ignores_missing_files(self, sandbox, tmp_path):
        assert save_manager.estimate_pdf_peak_bytes(
            [(str(tmp_path / "nope.jpg"), 0)]) == 0

    def test_confirm_called_when_peak_exceeds_limit(self, sandbox, jpg,
                                                    monkeypatch):
        """估算峰值超门禁 ⇒ 合并**之前**问用户，且给"仍然继续"的选项。"""
        monkeypatch.setattr(save_manager, "PDF_PEAK_LIMIT_BYTES", 1)
        asked = []

        def _confirm(msg):
            asked.append(msg)
            return True

        save_manager.save_pages("job_pre", "pdf", str(sandbox["cfg_dir"] + "/x.pdf"),
                                [(jpg, 0)], [[0]], confirm=_confirm)
        assert asked, "超门禁时必须在合并前提示用户"

    def test_declining_precheck_aborts_without_truncating(self, sandbox, jpg,
                                                           monkeypatch):
        """用户选择"取消"⇒ 放弃保存，**不静默截断**页数。"""
        monkeypatch.setattr(save_manager, "PDF_PEAK_LIMIT_BYTES", 1)
        with pytest.raises(save_manager.SaveAborted):
            save_manager.save_pages("job_pre2", "pdf",
                                    str(sandbox["cfg_dir"] + "/y.pdf"),
                                    [(jpg, 0)], [[0]], confirm=lambda msg: False)

    def test_no_confirm_callback_only_warns(self, sandbox, jpg, monkeypatch):
        """没有 GUI 回调时只记警告并继续（提示进 .warnings）。"""
        monkeypatch.setattr(save_manager, "PDF_PEAK_LIMIT_BYTES", 1)
        result = save_manager.save_pages("job_pre3", "pdf",
                                         str(sandbox["cfg_dir"] + "/z.pdf"),
                                         [(jpg, 0)], [[0]])
        assert result.warnings, "超门禁提示必须能被调用方取到"
        assert len(result) == 1

    def test_run_save_job_background_entry(self, sandbox, jpg):
        """后台入口：纯函数、返回 SaveResult，可直接放进 QRunnable。"""
        req = save_manager.SaveRequest(
            job_id="job_bg", ext="jpg",
            output_path=str(sandbox["cfg_dir"] + "/bg.jpg"),
            page_specs=[(jpg, 0)], groups=[[0]],
            device_name="HP M232", device_ip="1.2.3.4")
        result = save_manager.run_save_job(req)
        assert isinstance(result, save_manager.SaveResult)
        assert len(result) == 1
        assert os.path.exists(result[0])
        records = history_manager.list_records(limit=10)
        assert records and records[0]["device_name"] == "HP M232"


# ── F-7：history 读失败不覆盖原文件 ───────────────────────


class TestF7HistoryReadFailure:
    def test_oserror_backs_up_and_does_not_overwrite(self, sandbox, monkeypatch):
        """读历史抛 OSError（被独占）⇒ 有备份 + 原文件不被空列表覆盖。"""
        path = history_manager._history_file()
        original = '[{"device_ip": "1.2.3.4"}]'
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

        real_open = open

        def _boom(*args, **kwargs):
            if args and str(args[0]).endswith("history.json"):
                raise PermissionError("文件被其它程序独占")
            return real_open(*args, **kwargs)

        # 只拦 history_manager 模块自己的 open（备份走 shutil，用的是真 open）
        monkeypatch.setattr(history_manager, "open", _boom, raising=False)

        history_manager.append({"device_ip": "5.6.7.8"})  # 不得抛异常

        with real_open(path, encoding="utf-8") as f:
            assert f.read() == original, "读失败时原历史不得被覆盖"
        backups = [f for f in os.listdir(sandbox["cfg_dir"]) if ".corrupt-" in f]
        assert backups, "读失败也必须留下备份"

    def test_list_records_survives_read_failure(self, sandbox, monkeypatch):
        def _boom(*args, **kwargs):
            raise PermissionError("locked")

        monkeypatch.setattr(history_manager, "open", _boom, raising=False)
        assert history_manager.list_records() == []


# ── E-1：_a4_canvas 永不裁切 ──────────────────────────────


class TestE1NoCropping:
    def test_600dpi_source_is_not_cropped(self):
        """600 DPI（4960×7015）⇒ 等比缩放居中，四角内容必须还在。"""
        w, h = 4960, 7015
        img = Image.new("RGB", (w, h), "white")
        marker = 60
        for x0, y0 in ((0, 0), (w - marker, 0), (0, h - marker), (w - marker, h - marker)):
            for x in range(x0, x0 + marker):
                for y in range(y0, y0 + marker):
                    img.putpixel((x, y), (0, 0, 0))

        canvas = save_manager._a4_canvas(img)
        try:
            assert canvas.size == (2480, 3507)
            corners = [(5, 5), (2474, 5), (5, 3501), (2474, 3501)]
            for cx, cy in corners:
                pixel = canvas.getpixel((cx, cy))
                assert pixel != (255, 255, 255), \
                    f"角落 ({cx},{cy}) 被裁掉了：{pixel}"
        finally:
            canvas.close()

    def test_exact_a4_match_is_returned_unchanged(self):
        """300 DPI 精确匹配（2480×3507）⇒ 原样返回，不重编码、不改像素。"""
        img = Image.new("RGB", (2480, 3507), "white")
        for x in range(10, 20):
            for y in range(10, 20):
                img.putpixel((x, y), (255, 0, 0))
        canvas = save_manager._a4_canvas(img)
        try:
            assert canvas is img, "精确匹配时必须原样返回（逐字节不变）"
            assert canvas.size == (2480, 3507)
            assert canvas.getpixel((15, 15)) == (255, 0, 0)
        finally:
            if canvas is not img:
                canvas.close()

    def test_small_source_still_padded_and_centered(self):
        """源图小于画布 ⇒ 保持"留白居中"（既有行为不变）。"""
        img = Image.new("RGB", (100, 100), (10, 20, 30))
        canvas = save_manager._a4_canvas(img)
        try:
            assert canvas.size == (2480, 3507)
            assert canvas.getpixel((1240, 1753)) == (10, 20, 30)
            assert canvas.getpixel((0, 0)) == (255, 255, 255)
        finally:
            canvas.close()


# ── E-2：最终 PDF 原子写 ──────────────────────────────────


class TestE2AtomicPdf:
    def test_write_failure_leaves_no_final_file(self, sandbox, jpg, tmp_path,
                                                monkeypatch):
        real_writer = save_manager.PdfWriter

        class _BoomWriter(real_writer):
            def write(self, f):
                raise OSError("磁盘已满")

        monkeypatch.setattr(save_manager, "PdfWriter", _BoomWriter)
        out = tmp_path / "out.pdf"
        with pytest.raises(OSError):
            save_manager.save_single(jpg, "pdf", str(out))

        assert not out.exists(), "最终路径上不得留下半截 PDF"
        leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
        assert not leftovers, f"临时文件未清理: {leftovers}"

    def test_success_replaces_final_path(self, sandbox, jpg, tmp_path):
        out = tmp_path / "ok.pdf"
        saved = save_manager.save_single(jpg, "pdf", str(out))
        assert os.path.exists(saved)
        assert len(PdfReader(saved).pages) == 1
        leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
        assert not leftovers


# ── E-3：缓存大小不再每页全量遍历 ─────────────────────────


class TestE3CacheSizeScan:
    def test_write_page_does_not_walk_every_time(self, sandbox, monkeypatch):
        real_walk = os.walk
        calls = []

        def _counting_walk(*args, **kwargs):
            calls.append(args[0] if args else None)
            return real_walk(*args, **kwargs)

        monkeypatch.setattr(cache_manager.os, "walk", _counting_walk)
        for i in range(8):
            cache_manager.write_page("job_e3", i + 1, b"x" * 100, "jpg")

        assert len(calls) <= 2, f"全量遍历次数过多: {len(calls)}"

    def test_size_counter_stays_exact(self, sandbox):
        cache_manager.write_page("job_sz", 1, b"x" * 100, "jpg")
        assert cache_manager._cache_size_bytes() == 100
        cache_manager.write_page("job_sz", 2, b"x" * 50, "jpg")
        assert cache_manager._cache_size_bytes() == 150
        cache_manager.remove_job("job_sz")
        assert cache_manager._cache_size_bytes() == 0


# ── E-4：profile 缓存 TTL ─────────────────────────────────


class TestE4ProfileTTL:
    def test_external_change_visible_after_ttl(self, sandbox, monkeypatch):
        """TTL 语义：TTL 内读不到外部改动，TTL 过期后一定读得到。"""
        path = os.path.join(sandbox["cfg_dir"], "profiles.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"1.1.1.1": {"resolution": 300}}, f)

        assert profile_manager.get_profile("1.1.1.1") == {"resolution": 300}

        # 外部进程改了 profiles.json
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"2.2.2.2": {"resolution": 600}}, f)

        # TTL 内：缓存命中，读不到外部改动
        assert profile_manager.get_profile("2.2.2.2") == {}

        # TTL 过期：重新读盘 ⇒ 读得到
        profile_manager._profiles_cache_time = 0
        assert profile_manager.get_profile("2.2.2.2") == {"resolution": 600}

    def test_write_path_refreshes_cache(self, sandbox):
        profile_manager.save_profile("3.3.3.3", {"resolution": 200})
        assert profile_manager.get_profile("3.3.3.3") == {"resolution": 200}
        assert profile_manager.get_all_profiles()["3.3.3.3"] == {"resolution": 200}
