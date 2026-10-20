"""
GUI 一致性回归测试 — HP Scan Tool v4.0

覆盖三个「用户以为生效了、其实没生效 / 结果错了」的缺陷：

  I-5  缓存上限设置读写键名错配：GUI 写 "limit_mb"，cache_manager 读 "cache_limit_mb"
       ⇒ 用户改了上限保存后零影响。
  I-6  「直接保存（跳过预览）」复选框是空壳：勾选状态被读出但无人消费
       ⇒ 扫描完照样弹预览。
  I-7  多页预览删页后 splits 没跟着调整 ⇒ 最终保存时页被分进错误的组。

GUI 一律用 offscreen 跑，不弹真实窗口。
"""

import io
import os
import tempfile
import types
from pathlib import Path

import pytest
from PIL import Image

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
# 测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_gui_test_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import scan_coordinator
    import cache_manager
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)


@pytest.fixture(scope="module")
def qapp():
    """整个模块共用一个 QApplication（offscreen，不显示窗口）"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeMessageBox:
    """QMessageBox 替身：记录提示调用，绝不弹窗"""

    Yes = 0x00004000          # 与 QMessageBox.StandardButton.Yes 同值
    No = 0x00010000

    def __init__(self):
        self.critical_calls = []
        self.information_calls = []
        self.warning_calls = []
        self.question_reply = self.Yes

    def critical(self, parent, title, text):
        self.critical_calls.append((title, text))

    def information(self, parent, title, text):
        self.information_calls.append((title, text))

    def warning(self, parent, title, text):
        self.warning_calls.append((title, text))

    def question(self, parent, title, text):
        return self.question_reply


class _StubApp:
    """
    ScanApp 的最小替身：只提供 _on_scan_finished 用到的成员。

    真实 ScanApp.__init__ 会建协调器、加载界面、起定时器，测试里不合适，
    这里只借用 ScanApp 的方法实现（_on_scan_finished / _direct_save_result）。
    """

    _on_scan_finished = gui.ScanApp._on_scan_finished
    _direct_save_result = gui.ScanApp._direct_save_result

    def __init__(self, direct_save=False, selected=None, extra_dirs=None):
        self.direct_save_val = direct_save
        self.selected = selected
        self.extra_dirs = [] if extra_dirs is None else extra_dirs
        self.messages = []
        self.preview_calls = []
        self.status_bar = types.SimpleNamespace(showMessage=self.messages.append)

    def _reset_scan_ui(self):
        pass

    def _show_preview(self, *args, **kwargs):
        self.preview_calls.append(("single", args, kwargs))

    def _show_multi_preview(self, *args, **kwargs):
        self.preview_calls.append(("multi", args, kwargs))


def _make_result(tmp_path, page_count=3, ext="jpg"):
    """构造一个 ScanResult 替身（只含 GUI 用得到的字段）"""
    return types.SimpleNamespace(
        job_id="job_direct_save",
        ext=ext,
        page_count=page_count,
        cached_paths=[f"cache_page_{i:03d}.{ext}" for i in range(1, page_count + 1)],
        output_path=str(Path(tmp_path) / f"out.{ext}"),
    )


# ────────── I-5 缓存设置键名一致性 ──────────

def test_cache_setting_keys_match_cache_manager_defaults():
    """GUI 侧使用的缓存配置键名必须与 cache_manager._DEFAULTS 完全一致"""
    assert set(gui.CACHE_SETTING_KEYS) == set(cache_manager._DEFAULTS.keys())
    assert "cache_limit_mb" in cache_manager._DEFAULTS
    assert "limit_mb" not in cache_manager._DEFAULTS


def test_gui_cache_settings_saved_under_real_keys(tmp_config_dir):
    """GUI 保存的缓存上限必须落在真实键上，并能被 cache_manager 读回"""
    config = cache_manager.load_cache_config()
    gui.apply_cache_settings(config, 256, 90, 200)
    cache_manager.save_cache_config(config)

    loaded = cache_manager.load_cache_config()
    assert loaded["cache_limit_mb"] == 256        # 真实键；此前 GUI 写的是 limit_mb
    assert loaded["trigger_ratio"] == pytest.approx(0.9)
    assert loaded["cleanup_mb"] == 200
    assert "stale_hours" in loaded                # 同一份配置契约，缺省补齐
    assert "limit_mb" not in loaded               # 历史错配键已清掉
    assert set(gui.CACHE_SETTING_KEYS) <= set(loaded.keys())


def test_gui_saved_limit_actually_drives_eviction(isolated_cache, monkeypatch):
    """GUI 存进去的上限必须真的成为 cache_manager 的清理触发线"""
    if not hasattr(cache_manager, "_maybe_evict_locked"):
        pytest.skip("cache_manager 内部 API 变化，跳过")

    config = cache_manager.load_cache_config()
    gui.apply_cache_settings(config, 256, 95, 150)
    cache_manager.save_cache_config(config)

    evicted = []
    monkeypatch.setattr(cache_manager, "_evict_oldest_jobs_locked",
                        lambda target: evicted.append(target))
    monkeypatch.setattr(cache_manager, "_cache_size_bytes",
                        lambda: 200 * 1024 * 1024)
    cache_manager._maybe_evict_locked()
    assert evicted == []                          # 200MB < 256MB × 0.95

    monkeypatch.setattr(cache_manager, "_cache_size_bytes",
                        lambda: 300 * 1024 * 1024)
    cache_manager._maybe_evict_locked()
    assert len(evicted) == 1                      # 300MB > 256MB × 0.95 ⇒ 触发清理


# ────────── I-6 「直接保存」必须真的生效 ──────────

def test_direct_save_skips_preview_and_calls_save_pages(monkeypatch, tmp_path):
    """勾了「直接保存」⇒ 不弹预览，且确实调用 save_manager.save_pages"""
    fake_msgbox = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake_msgbox)

    calls = []

    def _fake_save_pages(*args, **kwargs):
        calls.append((args, kwargs))
        return [str(Path(tmp_path) / "out_001.jpg")]

    monkeypatch.setattr(gui.save_manager, "save_pages", _fake_save_pages)

    app = _StubApp(direct_save=True, selected=None)
    result = _make_result(tmp_path)
    app._on_scan_finished(result.job_id, result)

    assert app.preview_calls == [], "勾了直接保存就不该再弹预览"
    assert len(calls) == 1, "直接保存必须真的调用 save_manager.save_pages"

    args, kwargs = calls[0]
    assert args[0] == result.job_id
    assert args[1] == result.ext
    assert args[2] == result.output_path
    assert args[3] == [(p, 0) for p in result.cached_paths]   # 无旋转
    assert args[4] == [[0, 1, 2]]                            # 单组
    assert kwargs["extra_dirs"] == []
    assert kwargs["device_name"] == ""
    assert kwargs["device_ip"] == ""
    assert fake_msgbox.information_calls, "保存完要提示保存路径"
    assert "已保存到" in fake_msgbox.information_calls[0][1]


def test_direct_save_uses_selected_device_info(monkeypatch, tmp_path):
    """设备名/IP 取自当前选中设备；未选中设备时留空且不报错"""
    monkeypatch.setattr(gui, "QMessageBox", _FakeMessageBox())

    calls = []
    monkeypatch.setattr(gui.save_manager, "save_pages",
                        lambda *a, **k: calls.append((a, k)) or ["out.jpg"])

    scanner = types.SimpleNamespace(model="HP LaserJet MFP M232", ip="192.168.1.20")
    app = _StubApp(direct_save=True, selected=scanner)
    result = _make_result(tmp_path)
    app._on_scan_finished(result.job_id, result)

    _, kwargs = calls[0]
    assert kwargs["device_name"] == "HP LaserJet MFP M232"
    assert kwargs["device_ip"] == "192.168.1.20"


def test_direct_save_checkbox_reflects_saved_state(qapp):
    """复选框显示状态必须与实际生效状态一致（配置里勾了，框也要是勾的）"""
    from PySide6.QtWidgets import QCheckBox

    for saved in (True, False):
        check = QCheckBox("直接保存（跳过预览）")
        emitted = []
        check.stateChanged.connect(lambda state: emitted.append(state))

        stub = types.SimpleNamespace(direct_save_val=saved, direct_save_check=check)
        gui.ScanApp._sync_direct_save_checkbox(stub)

        assert check.isChecked() == saved, "复选框状态与实际生效状态不一致"
        assert emitted == [], "同步状态不应触发 stateChanged（避免启动时多写配置）"


def test_direct_save_unchecked_still_shows_preview(monkeypatch, tmp_path):
    """没勾「直接保存」⇒ 保持原来的弹预览行为，且不直接保存"""
    monkeypatch.setattr(gui, "QMessageBox", _FakeMessageBox())

    calls = []
    monkeypatch.setattr(gui.save_manager, "save_pages",
                        lambda *a, **k: calls.append((a, k)) or ["out.jpg"])

    app = _StubApp(direct_save=False)
    result = _make_result(tmp_path, page_count=1)
    app._on_scan_finished(result.job_id, result)

    assert app.preview_calls, "没勾直接保存时必须照旧弹预览"
    assert app.preview_calls[0][0] == "single"
    assert calls == [], "没勾直接保存时不应调用 save_manager.save_pages"


def test_direct_save_failure_is_not_silent(monkeypatch, tmp_path):
    """保存失败必须弹错误提示，不能静默吞掉"""
    fake_msgbox = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake_msgbox)

    def _boom(*args, **kwargs):
        raise RuntimeError("磁盘写入失败")

    monkeypatch.setattr(gui.save_manager, "save_pages", _boom)

    app = _StubApp(direct_save=True)
    result = _make_result(tmp_path)
    app._on_scan_finished(result.job_id, result)

    assert fake_msgbox.critical_calls, "保存失败必须弹错误提示"
    assert not fake_msgbox.information_calls


# ────────── I-7 删页后 splits 必须同步调整 ──────────

def test_adjust_splits_matches_coordinator(isolated_cache):
    """GUI 的分割点调整语义必须与 scan_coordinator.delete_cached_page 一致"""
    coord_fn = getattr(scan_coordinator, "delete_cached_page", None)
    if coord_fn is None:
        pytest.skip("scan_coordinator.delete_cached_page 已被移除，跳过一致性断言")

    cases = [({0, 2, 4}, 2), ({1, 3}, 0), ({2}, 1), ({5}, 5), (set(), 2), ({0}, 0)]
    for splits, idx in cases:
        expected = set(coord_fn("job_parity", idx, "jpg", sorted(splits)))
        actual = gui.adjust_splits_after_delete(splits, idx)
        assert actual == expected, f"splits={splits} page_idx={idx} 与协调器语义不一致"


def test_delete_page_updates_splits_and_groups(qapp, isolated_cache, tmp_path,
                                               monkeypatch):
    """删掉中间某页后：页列表、splits、分组都要跟着调整"""
    fake_msgbox = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake_msgbox)

    job_id = "job_delete_page"
    for i in range(1, 6):
        buf = io.BytesIO()
        Image.new("RGB", (60, 80), (200, 220, 240)).save(buf, format="JPEG")
        cache_manager.write_page(job_id, i, buf.getvalue(), "jpg")

    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                     str(Path(tmp_path) / "out.jpg"), 5)
    try:
        dlg.splits = {2, 4}
        dlg.selected_idx = 2          # 删掉第 3 页
        dlg._delete_current_page()

        assert dlg.pages == [1, 2, 4, 5]        # 被删页已移出列表
        assert 3 not in dlg.pages
        assert dlg.splits == {3}                # 落在被删页上的分割点移除，其后的前移

        groups = dlg._build_groups()
        flat = [idx for g in groups for idx in g]
        assert flat == list(range(len(dlg.pages))), "分组里不能含被删页"
        assert groups == [[0, 1, 2], [3]]
        assert [[dlg.pages[i] for i in g] for g in groups] == [[1, 2, 4], [5]]
    finally:
        dlg.close()
