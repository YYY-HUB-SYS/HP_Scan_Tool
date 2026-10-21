"""
多页预览「逐页曝光覆盖」—— 回归测试（HP Scan Tool v4.0）

★ 背景
---------------------------------------------------------------------------
文档级曝光（`MultiPagePreviewDialog.exposure_panel`）作用于**全部页**；
用户还需要「**这一页单独调曝光**」（合同页比别的页亮一点之类）。

★ 本轮实现（GUI 侧；落盘侧 `save_manager` 已就绪）
  · `MultiPagePreviewDialog._page_exposure`：**页号 → 该页的显式曝光覆盖**
    （只装**有覆盖**的页；值**永远不是 None**）
  · UI：第二个 `ExposurePanel`（折叠头「本页曝光」）+「本页单独设置」/「跟随整批」
    + 只读状态标签
  · 预览用**当前页生效**的曝光（`_current_exposure`）
  · 保存时按 `self.pages` 同序收集进 `SaveRequest.page_exposures`
    （与 `page_specs` **下标天然对齐**；没有覆盖的页为 None ⇒ 跟随整批）

★ 关键语义（下面的用例把它钉死）
  1. `ExposurePanel.exposure_dict()` 在 mode=off 时返回 **None**
     ⇒ 落盘侧的 None 表示「跟随整批」⇒ 覆盖必须存**显式的关闭态 dict**
  2. 换页时本页面板必须回填成**那一页**的覆盖（不是前一页的）
  3. `page_exposures` 与 `self.pages` **下标对齐**（不排序、不去重）

★ 全部 offscreen 跑：**绝不连真机、绝不弹真窗、绝不出纸**。
"""

import io
import os
import tempfile
import time

import pytest
from PIL import Image, ImageChops
from PySide6.QtCore import QEvent

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_gui_page_exposure_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import cache_manager
    import save_manager
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)


@pytest.fixture(scope="module")
def qapp(qt_app):
    """整个模块共用一个 QApplication（offscreen，不显示窗口）"""
    return qt_app


def _flush(qapp):
    """把 deleteLater 的控件真正销毁掉"""
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def _make_preview(qapp, tmp_path, page_count=2, ext="jpg", job_id=None):
    """造一个带**真实缓存页**的多页预览对话框（预览渲染能真的跑起来）"""
    job_id = job_id or ("job_pe_%s_%d" % (ext, page_count))
    fmt = "JPEG" if ext in ("jpg", "jpeg") else "PNG"
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), (200, 220, 240)).save(buf, fmt)
    data = buf.getvalue()
    for page in range(1, page_count + 1):
        cache_manager.write_page(job_id, page, data, ext)
    dlg = gui.MultiPagePreviewDialog(None, job_id, ext,
                                     str(tmp_path / ("out." + ext)), page_count)
    _flush(qapp)
    return dlg


def _set_panel(panel, mode, **sliders):
    """
    设置某个 ExposurePanel 的模式与滑块，并**立即**触发 params_changed。

    ★ 滑块有 200ms 防抖（`ExposurePanel.DEBOUNCE_MS`）⇒ 测试里不能干等，
      直接调 `_emit_params_changed()` 走同一条信号路径。
    """
    idx = panel.mode_combo.findData(mode)
    assert idx >= 0, "曝光面板没有 %r 模式" % (mode,)
    panel.mode_combo.setCurrentIndex(idx)
    for key, value in sliders.items():
        panel._sliders[key][0].setValue(value)
    panel._emit_params_changed()


class _SaveProbe:
    """拦截落盘入口：记录 SaveRequest（**绝不真写盘**）。"""

    def __init__(self):
        self.requests = []

    def run_save_job(self, req):
        self.requests.append(req)
        return save_manager.SaveResult([])

    def run_in_background(self, work, on_done, on_error, label="background"):
        try:
            on_done(work())
        except Exception as e:            # pragma: no cover - 防御
            on_error(e)


@pytest.fixture
def save_probe(monkeypatch):
    probe = _SaveProbe()
    monkeypatch.setattr(save_manager, "run_save_job", probe.run_save_job)
    monkeypatch.setattr(gui, "_run_in_background", probe.run_in_background)
    monkeypatch.setattr(gui, "_open_folder_after_save", lambda *a, **k: False)
    return probe


class TestPageExposureState:
    """「本页曝光」控件与状态字典"""

    def test_panel_starts_following_batch(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path)
        try:
            assert dlg.page_exposure_state_label.text() == "本页曝光：跟随整批"
            assert dlg.page_exposure_panel.header_btn.text() == "本页曝光"
            assert dlg._page_exposure == {}
        finally:
            dlg.deleteLater()

    def test_detach_creates_override(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path)
        try:
            dlg.page_exposure_single_btn.click()
            assert 1 in dlg._page_exposure, "当前页没有进 _page_exposure"
            assert isinstance(dlg._page_exposure[1], dict)
            assert dlg.page_exposure_state_label.text() == "本页曝光：自定义"
        finally:
            dlg.deleteLater()

    def test_follow_removes_override(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path)
        try:
            dlg.page_exposure_single_btn.click()
            assert dlg._page_exposure
            dlg.page_exposure_follow_btn.click()
            assert dlg._page_exposure == {}
            assert dlg.page_exposure_state_label.text() == "本页曝光：跟随整批"
        finally:
            dlg.deleteLater()

    def test_detach_seeds_from_batch_exposure(self, qapp, tmp_path, isolated_cache):
        """「本页单独设置」的初值 = 整批当前曝光"""
        dlg = _make_preview(qapp, tmp_path)
        try:
            _set_panel(dlg.exposure_panel, "manual", contrast=25)
            dlg.page_exposure_single_btn.click()
            override = dlg._page_exposure[1]
            assert override["mode"] == "manual"
            assert override["contrast"] == 25
        finally:
            dlg.deleteLater()

    def test_detach_without_batch_exposure_uses_neutral_seed(self, qapp, tmp_path,
                                                             isolated_cache):
        """整批没开曝光 ⇒ 用中性值起头（而不是 None）"""
        dlg = _make_preview(qapp, tmp_path)
        try:
            assert dlg.exposure_panel.exposure_dict() is None
            dlg.page_exposure_single_btn.click()
            override = dlg._page_exposure[1]
            assert isinstance(override, dict)
            assert override["mode"] == "manual"
            assert override["brightness"] == 0
            assert override["gamma"] == 1.0
        finally:
            dlg.deleteLater()

    def test_off_mode_stores_explicit_dict(self, qapp, tmp_path, isolated_cache):
        """
        ★ mode=off ⇒ `exposure_dict()` 返回 None；而落盘侧的 None 表示
        「跟随整批」⇒ 这里必须存**显式的关闭态 dict**，否则「这一页不套曝光」
        会被静默降级成「跟随整批」。
        """
        dlg = _make_preview(qapp, tmp_path)
        try:
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "off")
            override = dlg._page_exposure[1]
            assert override is not None, "off 态被存成了 None（= 跟随整批）"
            assert isinstance(override, dict)
            assert override["mode"] == "off"
        finally:
            dlg.deleteLater()

    def test_panel_changes_are_written_back(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path)
        try:
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=40)
            assert dlg._page_exposure[1]["brightness"] == 40
            assert dlg._page_exposure[1]["mode"] == "manual"
        finally:
            dlg.deleteLater()


class TestPageSwitchBackfill:
    """换页时面板必须回填成**那一页**的覆盖"""

    def test_switch_to_page_without_override_clears_panel(self, qapp, tmp_path,
                                                           isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=2)
        try:
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=40)
            dlg._on_thumb_clicked(1)
            assert dlg.page_exposure_panel.mode_combo.currentData() == "off", \
                "第 2 页没有覆盖 ⇒ 面板应回填成关闭态"
            assert 2 not in dlg._page_exposure
            assert dlg.page_exposure_state_label.text() == "本页曝光：跟随整批"
        finally:
            dlg.deleteLater()

    def test_switch_back_restores_that_page_override(self, qapp, tmp_path,
                                                      isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=2)
        try:
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=40)
            dlg._on_thumb_clicked(1)
            dlg._on_thumb_clicked(0)
            assert dlg.page_exposure_panel.mode_combo.currentData() == "manual"
            assert dlg.page_exposure_panel._sliders["brightness"][0].value() == 40
            assert dlg.page_exposure_state_label.text() == "本页曝光：自定义"
        finally:
            dlg.deleteLater()

    def test_each_page_keeps_its_own_override(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=2)
        try:
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=40)
            dlg._on_thumb_clicked(1)
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=70)
            assert dlg._page_exposure[1]["brightness"] == 40
            assert dlg._page_exposure[2]["brightness"] == 70
        finally:
            dlg.deleteLater()

    def test_backfill_does_not_write_back(self, qapp, tmp_path, isolated_cache):
        """回填走 `set_exposure`（抑制信号）⇒ 不得凭空造出覆盖"""
        dlg = _make_preview(qapp, tmp_path, page_count=2)
        try:
            dlg._on_thumb_clicked(1)
            dlg._on_thumb_clicked(0)
            assert dlg._page_exposure == {}, "回填误写了 _page_exposure"
        finally:
            dlg.deleteLater()


class TestPreviewUsesPageOverride:
    """预览必须显示**当前页覆盖后**的效果"""

    def test_preview_uses_page_override_not_batch(self, qapp, tmp_path,
                                                  isolated_cache, monkeypatch):
        recorded = []
        real_apply = gui.apply_exposure

        def _spy(img, **kwargs):
            recorded.append(kwargs)
            return real_apply(img, **kwargs)

        monkeypatch.setattr(gui, "apply_exposure", _spy)

        dlg = _make_preview(qapp, tmp_path)
        try:
            _set_panel(dlg.exposure_panel, "manual", brightness=11)
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=77)

            recorded.clear()
            dlg._recompute_preview()
            assert recorded, "预览没有走 apply_exposure"
            assert recorded[-1]["brightness"] == 77, \
                "预览用的是整批曝光（%r），不是当前页的覆盖" % (recorded[-1],)
        finally:
            dlg.deleteLater()

    def test_preview_follows_batch_when_no_override(self, qapp, tmp_path,
                                                    isolated_cache, monkeypatch):
        recorded = []
        real_apply = gui.apply_exposure

        def _spy(img, **kwargs):
            recorded.append(kwargs)
            return real_apply(img, **kwargs)

        monkeypatch.setattr(gui, "apply_exposure", _spy)

        dlg = _make_preview(qapp, tmp_path, page_count=2)
        try:
            _set_panel(dlg.exposure_panel, "manual", brightness=11)
            recorded.clear()
            dlg._recompute_preview()
            assert recorded[-1]["brightness"] == 11

            # 切到没有覆盖的第 2 页 ⇒ 跟随整批
            dlg._on_thumb_clicked(1)
            recorded.clear()
            dlg._recompute_preview()
            assert recorded[-1]["brightness"] == 11
        finally:
            dlg.deleteLater()

    def test_current_exposure_prefers_override(self, qapp, tmp_path,
                                                isolated_cache):
        dlg = _make_preview(qapp, tmp_path)
        try:
            _set_panel(dlg.exposure_panel, "manual", brightness=11)
            assert dlg._current_exposure()["brightness"] == 11
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=77)
            assert dlg._current_exposure()["brightness"] == 77
        finally:
            dlg.deleteLater()


class TestSaveRequestPageExposures:
    """`page_exposures` 与 `self.pages` 下标对齐"""

    def test_all_none_without_override(self, qapp, tmp_path, isolated_cache,
                                       save_probe):
        dlg = _make_preview(qapp, tmp_path, page_count=3)
        try:
            dlg._confirm()
            req = save_probe.requests[0]
            assert req.page_exposures == [None, None, None]
        finally:
            dlg.deleteLater()

    def test_overrides_are_aligned_with_pages(self, qapp, tmp_path,
                                               isolated_cache, save_probe):
        dlg = _make_preview(qapp, tmp_path, page_count=3)
        try:
            # 第 2 页：亮度 55
            dlg._on_thumb_clicked(1)
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=55)
            # 第 3 页：亮度 66
            dlg._on_thumb_clicked(2)
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=66)

            dlg._confirm()
            req = save_probe.requests[0]
            assert dlg.pages == [1, 2, 3]
            assert len(req.page_exposures) == len(dlg.pages)
            assert req.page_exposures[0] is None, "第 1 页没有覆盖 ⇒ 必须是 None"
            assert req.page_exposures[1]["brightness"] == 55
            assert req.page_exposures[2]["brightness"] == 66
        finally:
            dlg.deleteLater()

    def test_off_override_is_transmitted_as_dict(self, qapp, tmp_path,
                                                 isolated_cache, save_probe):
        """「本页明确不套曝光」必须以 dict 传下去（None 会被当成跟随整批）"""
        dlg = _make_preview(qapp, tmp_path, page_count=2)
        try:
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "off")
            dlg._confirm()
            req = save_probe.requests[0]
            assert req.page_exposures[0] is not None
            assert req.page_exposures[0]["mode"] == "off"
            assert req.page_exposures[1] is None
        finally:
            dlg.deleteLater()

    def test_single_page_path_uses_page_override(self, qapp, tmp_path,
                                                  isolated_cache, monkeypatch,
                                                  save_probe):
        """单页单组走 `_save_page_specs` ⇒ 这一页的覆盖必须生效"""
        captured = {}

        def _spy(job_id, ext, output_path, page_specs, groups, **kwargs):
            captured.update(kwargs)
            return save_manager.SaveResult([])

        monkeypatch.setattr(gui, "_save_page_specs", _spy)

        dlg = _make_preview(qapp, tmp_path, page_count=1)
        try:
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=33)
            dlg._confirm()
            assert captured.get("exposure"), "单页保存没有收到曝光参数"
            assert captured["exposure"]["brightness"] == 33, captured["exposure"]
        finally:
            dlg.deleteLater()


class TestPageExposureEndToEnd:
    """端到端：单页覆盖 ⇒ 产物像素真的变了"""

    def test_page_override_changes_saved_pixels(self, qapp, tmp_path,
                                                isolated_cache, monkeypatch):
        monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
        dlg = _make_preview(qapp, tmp_path, page_count=2, ext="png",
                            job_id="job_pe_e2e")
        try:
            # 第 1 页单独提亮
            dlg.page_exposure_single_btn.click()
            _set_panel(dlg.page_exposure_panel, "manual", brightness=60)
            dlg._confirm()
            deadline = time.time() + 15.0
            while time.time() < deadline and not dlg._save_finished:
                qapp.processEvents()
                time.sleep(0.01)
            assert dlg._save_finished, "保存没结束"
        finally:
            dlg.deleteLater()

        outs = sorted(tmp_path.glob("out*.png"))
        assert len(outs) == 2, "产物数量不对: %r" % (outs,)
        with Image.open(outs[0]) as a, Image.open(outs[1]) as b:
            first = a.convert("RGB")
            second = b.convert("RGB")
            assert first.size == second.size
            # ★ 两页源图完全一样 ⇒ 没有逐页覆盖时产物应逐像素相同
            box = ImageChops.difference(first, second).getbbox()
            assert box is not None, "两页产物完全一样（逐页曝光没生效）"
            # 提亮的那一页在差异区域里应当更亮
            first_mean = sum(first.crop(box).tobytes()) / \
                len(first.crop(box).tobytes())
            second_mean = sum(second.crop(box).tobytes()) / \
                len(second.crop(box).tobytes())
        assert first_mean > second_mean, \
            "单独提亮的那一页并不更亮（%r vs %r）" % (first_mean, second_mean)
