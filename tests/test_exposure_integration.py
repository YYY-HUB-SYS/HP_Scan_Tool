"""
曝光集成测试 — HP Scan Tool v4.0

覆盖需求文档《2026-10-20_曝光GUI接入_需求_v1.0.md》§4 的 5 条落盘断言
+ 3 条 GUI 断言：

  1. save_single(..., exposure={"mode":"manual","brightness":50}) ⇒ 产物确实更亮
  2. exposure=None ⇒ 产物与不加曝光时**逐字节相同**（向后兼容）
  3. mode="auto" 对合成灰底图 ⇒ 落盘产物对比度更高
  4. 曝光在 **A4 归一之前** 应用 ⇒ A4 画布白边仍然纯白
  5. 曝光参数从 GUI 一路传到 save_manager（mock 断言收到 exposure 字典）

  GUI 侧（offscreen，绝不弹真实窗口）：
  - 模式切换能改曝光状态
  - 拖动滑块触发重算（mock 断言 apply_exposure 被调用、参数正确）
  - auto 模式重算走工作线程（断言回调最终在主线程执行）
"""

import os
import tempfile
import threading
import time

import pytest
from PIL import Image, ImageChops

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
# 测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_exposure_test_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import cache_manager
    import history_manager
    import preset_manager
    import save_manager
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


# ────────── 素材 ──────────

@pytest.fixture
def gray_src(tmp_path):
    """灰底 + 局部深色方块的合成图（auto 去灰底的典型输入）"""
    img = Image.new("RGB", (400, 300), (200, 200, 200))
    img.paste((40, 40, 40), (100, 100, 200, 200))
    p = tmp_path / "gray.png"
    img.save(p, "PNG")
    return str(p)


def _histogram_range(path):
    """落盘图片的灰度动态范围（max - min）——「对比度」的直观度量"""
    with Image.open(path) as im:
        hist = im.convert("L").histogram()
    nonzero = [i for i, count in enumerate(hist) if count]
    return nonzero[-1] - nonzero[0]


# ────────── 落盘侧 ──────────

class TestSaveSingleExposure:
    def test_manual_brightness_makes_output_brighter(self, tmp_path, tmp_config_dir):
        """曝光参数确实作用到了产物像素上（不是只在预览里好看）。"""
        src = tmp_path / "src.png"
        Image.new("RGB", (120, 160), (120, 120, 120)).save(src, "PNG")

        plain = tmp_path / "plain.png"
        bright = tmp_path / "bright.png"
        save_manager.save_single(str(src), "png", str(plain))
        save_manager.save_single(str(src), "png", str(bright),
                                 exposure={"mode": "manual", "brightness": 50})

        with Image.open(plain) as a, Image.open(bright) as b:
            a_rgb, b_rgb = a.convert("RGB"), b.convert("RGB")
            diff = ImageChops.difference(a_rgb, b_rgb)
            assert diff.getbbox() is not None, "加了曝光但产物像素没有任何变化"
            assert b_rgb.getpixel((10, 10))[0] > a_rgb.getpixel((10, 10))[0], \
                "brightness=50 之后产物没有变亮"

    def test_exposure_none_is_byte_identical(self, tmp_path, tmp_config_dir):
        """exposure=None ⇒ 与不加曝光逐字节相同（向后兼容，老测试一条不退化）。"""
        src = tmp_path / "src.png"
        Image.new("RGB", (80, 60), (90, 110, 130)).save(src, "PNG")

        base = tmp_path / "base.png"
        none = tmp_path / "none.png"
        off = tmp_path / "off.png"
        save_manager.save_single(str(src), "png", str(base))
        save_manager.save_single(str(src), "png", str(none), exposure=None)
        save_manager.save_single(str(src), "png", str(off), exposure={"mode": "off"})

        assert base.read_bytes() == none.read_bytes(), "exposure=None 改变了产物字节"
        assert base.read_bytes() == off.read_bytes(), "mode=off 改变了产物字节"

    def test_auto_mode_increases_contrast(self, tmp_path, tmp_config_dir, gray_src):
        """auto（去灰底）落盘产物对比度更高。"""
        plain = tmp_path / "plain.png"
        auto = tmp_path / "auto.png"
        save_manager.save_single(gray_src, "png", str(plain))
        save_manager.save_single(gray_src, "png", str(auto), exposure={"mode": "auto"})

        assert _histogram_range(str(auto)) > _histogram_range(str(plain)), \
            "auto 去灰底之后动态范围没有变大"

        with Image.open(plain) as p, Image.open(auto) as a:
            # 灰底必须被提亮（这才是 auto 的实际作用）
            assert a.convert("RGB").getpixel((10, 10))[0] > \
                p.convert("RGB").getpixel((10, 10))[0], "auto 之后灰底没有被提亮"

    def test_auto_ignores_manual_params(self, tmp_path, tmp_config_dir, gray_src):
        """auto 与手动互斥：mode=auto 时忽略 brightness 等手动参数。"""
        auto_plain = tmp_path / "auto_plain.png"
        auto_bright = tmp_path / "auto_bright.png"
        save_manager.save_single(gray_src, "png", str(auto_plain),
                                 exposure={"mode": "auto"})
        save_manager.save_single(gray_src, "png", str(auto_bright),
                                 exposure={"mode": "auto", "brightness": 80})
        assert auto_plain.read_bytes() == auto_bright.read_bytes()

    def test_exposure_recorded_in_history(self, tmp_path, tmp_config_dir):
        """历史记录里带上曝光参数，便于追溯。"""
        import json
        _, hist = tmp_config_dir, None
        src = tmp_path / "src.png"
        Image.new("RGB", (40, 40), "white").save(src, "PNG")
        save_manager.save_single(str(src), "png", str(tmp_path / "out.png"),
                                 exposure={"mode": "manual", "brightness": 10})
        hist_path = os.path.join(tmp_config_dir, "history.json")
        with open(hist_path, encoding="utf-8") as f:
            records = json.load(f)
        assert records[-1]["exposure"]["mode"] == "manual"
        assert records[-1]["exposure"]["brightness"] == 10


class TestA4Ordering:
    """曝光 → 旋转 → A4 归一，顺序是硬约定。"""

    def test_exposure_applied_before_a4_normalize(self, tmp_path, tmp_config_dir,
                                                  gray_src):
        """
        曝光在 A4 归一之前应用。

        端到端：落盘 PDF 里，内容区必须已经是接近纯白（>=245）。
        若顺序反了（先归一再曝光），白边会参与背景估计，内容区只能到 ~207。
        """
        from pypdf import PdfReader

        out = tmp_path / "out.pdf"
        save_manager.save_single(gray_src, "pdf", str(out), exposure={"mode": "auto"})

        reader = PdfReader(str(out))
        page = reader.pages[0]
        xobjects = page["/Resources"]["/XObject"]
        data = list(xobjects.values())[0].get_data()
        blob = tmp_path / "embedded.bin"
        blob.write_bytes(data)
        with Image.open(blob) as im:
            rendered = im.convert("RGB")

        content = rendered.getpixel((1240, 1750))
        corner = rendered.getpixel((5, 5))
        assert content[0] >= 245, \
            f"内容区只有 {content} —— 曝光多半是作用在 A4 画布上而不是原图上"
        assert corner[0] >= 254, f"白边被压暗成 {corner} —— 白边不该参与曝光"

    def test_a4_border_stays_pure_white(self, tmp_path, tmp_config_dir, gray_src):
        """白盒断言：A4 画布的白边逐像素是纯白（255,255,255）。"""
        img = save_manager._open_page_image(gray_src, 0, {"mode": "auto"})
        canvas = save_manager._a4_canvas(img, 300)
        try:
            assert canvas.getpixel((5, 5)) == (255, 255, 255)
            assert canvas.getpixel((canvas.size[0] - 5, 5)) == (255, 255, 255)
        finally:
            canvas.close()

        # 对照组：顺序反了白边就不是纯白 —— 证明上面那个断言真的有区分度
        with Image.open(gray_src) as src:
            raw = src.copy()
        wrong = save_manager._exposure_params({"mode": "auto"}) is not None
        assert wrong, "对照组前提失效"
        from exposure import apply_exposure
        reversed_canvas = apply_exposure(save_manager._a4_canvas(raw, 300), mode="auto")
        try:
            assert reversed_canvas.getpixel((5, 5)) != (255, 255, 255), \
                "对照组不再有区分度，顺序断言失去意义"
        finally:
            reversed_canvas.close()

    def test_rotation_applied_after_exposure(self, tmp_path, tmp_config_dir):
        """顺序固定：曝光 → 旋转。旋转只改几何，不改像素。"""
        src = tmp_path / "src.png"
        Image.new("RGB", (200, 100), (200, 200, 200)).save(src, "PNG")

        no_rot = save_manager._open_page_image(str(src), 0, {"mode": "auto"})
        rotated = save_manager._open_page_image(str(src), 90, {"mode": "auto"})
        try:
            assert no_rot.size == (200, 100)
            assert rotated.size == (100, 200), "旋转没有生效"
            # 旋转后的左上角应等于原图旋转后的对应位置：纯几何变换
            assert rotated.getpixel((0, 0)) == no_rot.getpixel((0, 0))
        finally:
            no_rot.close()
            rotated.close()


class TestSavePagesExposure:
    def test_pages_applied_exposure(self, tmp_path, tmp_config_dir):
        """多页保存：每一页都应用同一份（文档级）曝光参数。"""
        src = tmp_path / "src.png"
        Image.new("RGB", (60, 60), (100, 100, 100)).save(src, "PNG")

        plain = tmp_path / "plain.png"
        bright = tmp_path / "bright.png"
        save_manager.save_pages("job_a", "png", str(plain),
                                [(str(src), 0)] * 2, [[0, 1]])
        save_manager.save_pages("job_b", "png", str(bright),
                                [(str(src), 0)] * 2, [[0, 1]],
                                exposure={"mode": "manual", "brightness": 40})

        files = sorted(os.listdir(tmp_path))
        assert files, "没有产出文件"
        plain_files = [f for f in files if f.startswith("plain")]
        bright_files = [f for f in files if f.startswith("bright")]
        assert len(plain_files) == 2 and len(bright_files) == 2
        for pf, bf in zip(sorted(plain_files), sorted(bright_files)):
            with Image.open(tmp_path / pf) as a, Image.open(tmp_path / bf) as b:
                assert b.convert("RGB").getpixel((5, 5))[0] > \
                    a.convert("RGB").getpixel((5, 5))[0]


# ────────── GUI 侧 ──────────

class _FakeMessageBox:
    """QMessageBox 替身：记录提示调用，绝不弹窗"""
    Yes = 0x00004000
    No = 0x00010000

    def __init__(self):
        self.critical_calls = []
        self.information_calls = []
        self.warning_calls = []

    def critical(self, parent, title, text):
        self.critical_calls.append((title, text))

    def information(self, parent, title, text):
        self.information_calls.append((title, text))

    def warning(self, parent, title, text):
        self.warning_calls.append((title, text))

    def question(self, parent, title, text):
        return self.No


@pytest.fixture
def no_popups(monkeypatch):
    """所有可能弹窗的地方都换成替身（绝不弹真实窗口）"""
    fake = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake)
    monkeypatch.setattr(gui.QMessageBox, "critical", staticmethod(fake.critical))
    monkeypatch.setattr(gui.QMessageBox, "information", staticmethod(fake.information))
    monkeypatch.setattr(gui.QMessageBox, "warning", staticmethod(fake.warning))
    monkeypatch.setattr(gui.QMessageBox, "question", staticmethod(lambda *a, **k: fake.No))
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    return fake


def _wait_for(predicate, timeout=10.0, qapp=None):
    """泵事件循环直到 predicate 为真（超时返回 False）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        if qapp is not None:
            qapp.processEvents()
        time.sleep(0.01)
    return predicate()


class TestExposurePanel:
    def test_mode_switch_changes_exposure_state(self, qapp):
        """模式三选一：关闭 / 自动 / 手动，切换要改曝光状态。"""
        panel = gui.ExposurePanel()
        assert panel.exposure_dict() is None, "默认应当是关闭"

        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("manual"))
        assert panel.exposure_dict()["mode"] == "manual"

        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("auto"))
        assert panel.exposure_dict()["mode"] == "auto"

        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("off"))
        assert panel.exposure_dict() is None

    def test_builtin_presets_listed_and_not_deletable(self, qapp):
        """4 个内置预设在列表里，且内置的不可删。"""
        panel = gui.ExposurePanel()
        names = [panel.preset_combo.itemText(i)
                 for i in range(panel.preset_combo.count())]
        for builtin in ("文字增强", "照片还原", "去灰底", "旧文件修复"):
            assert builtin in names, f"内置预设缺失: {builtin}"

        idx = panel.preset_combo.findData("文字增强")
        panel.preset_combo.setCurrentIndex(idx)
        assert panel.delete_preset_btn.isEnabled() is False, "内置预设不该可删"

    def test_preset_applies_all_params(self, qapp):
        """选中预设 ⇒ 套用该预设的全部参数。"""
        panel = gui.ExposurePanel()
        idx = panel.preset_combo.findData("旧文件修复")
        panel.preset_combo.setCurrentIndex(idx)

        params = panel.exposure_dict()
        assert params["mode"] == "manual"
        assert params["brightness"] == 20
        assert params["contrast"] == 30
        assert params["gamma"] == 0.9
        assert params["shadows"] == -30
        assert params["highlights"] == 20
        assert params["channel_gains"] == (0.95, 1.0, 1.1)

    def test_slider_ranges_match_spec(self, qapp):
        """滑块量程必须覆盖需求文档给的参数域（gamma 0.1~3.0、通道增益 0.5~1.5）。"""
        panel = gui.ExposurePanel()

        def param_range(key):
            slider, scale = panel._sliders[key]
            return slider.minimum() * scale, slider.maximum() * scale

        assert param_range("brightness") == (-100, 100)
        assert param_range("contrast") == (-100, 100)
        assert param_range("gamma") == (0.1, 3.0)
        assert param_range("shadows") == (-100, 100)
        assert param_range("highlights") == (-100, 100)
        for key in ("r_gain", "g_gain", "b_gain"):
            assert param_range(key) == (0.5, 1.5)

        # 默认值：gamma=1.0、通道增益=1.0（不是 10.0）
        assert panel._params["gamma"] == 1.0
        assert panel._params["channel_gains"] == [1.0, 1.0, 1.0]

    def test_preset_gamma_survives_other_slider_change(self, qapp):
        """套用预设后再拖别的滑块，预设里的 gamma 不能被冲掉。"""
        panel = gui.ExposurePanel()
        idx = panel.preset_combo.findData("旧文件修复")
        panel.preset_combo.setCurrentIndex(idx)
        assert panel.exposure_dict()["gamma"] == 0.9

        panel._sliders["contrast"][0].setValue(55)
        assert panel.exposure_dict()["gamma"] == 0.9, \
            "拖别的滑块把预设里的 gamma 冲掉了"
        assert panel.exposure_dict()["contrast"] == 55

    def test_user_preset_saved_and_deleted(self, qapp, tmp_path, monkeypatch):
        """保存当前为预设 / 删除预设（仅用户自定义可删）。"""
        presets_file = tmp_path / "presets.json"
        monkeypatch.setattr(preset_manager, "_presets_path", lambda: str(presets_file))

        panel = gui.ExposurePanel()
        panel.mode_combo.setCurrentIndex(panel.mode_combo.findData("manual"))
        panel._sliders["contrast"][0].setValue(33)

        preset = {
            "name": "我的预设",
            "mode": "manual",
            "brightness": 0, "contrast": 33, "gamma": 1.0,
            "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        }
        preset_manager.save_user_preset(preset)

        panel.reload_presets()
        idx = panel.preset_combo.findData("我的预设")
        assert idx >= 0, "用户预设没进下拉"
        panel.preset_combo.setCurrentIndex(idx)
        assert panel.exposure_dict()["contrast"] == 33
        assert panel.delete_preset_btn.isEnabled() is True

        preset_manager.delete_user_preset("我的预设")
        panel.reload_presets()
        assert panel.preset_combo.findData("我的预设") < 0, "用户预设没被删掉"


class TestPreviewDialogExposure:
    def test_slider_drag_triggers_recompute(self, qapp, tmp_path, monkeypatch):
        """拖动滑块触发重算，参数要正确传给 apply_exposure。"""
        calls = []
        real_apply = gui.apply_exposure

        def _spy(img, **kwargs):
            calls.append(kwargs)
            return real_apply(img, **kwargs)

        monkeypatch.setattr(gui, "apply_exposure", _spy)

        src = tmp_path / "src.png"
        Image.new("RGB", (100, 100), (128, 128, 128)).save(src, "PNG")

        dlg = gui.PreviewDialog(None, str(src), "png",
                                str(tmp_path / "out.png"), "job_slider")
        try:
            dlg.exposure_panel.mode_combo.setCurrentIndex(
                dlg.exposure_panel.mode_combo.findData("manual"))
            dlg.exposure_panel._sliders["brightness"][0].setValue(42)

            # 滑块变化有 200ms 防抖 ⇒ 等重算真的发生
            assert _wait_for(lambda: any(c.get("brightness") == 42 for c in calls),
                             qapp=qapp), "拖滑块没有触发重算"
            last = [c for c in calls if c.get("brightness") == 42][-1]
            assert last["mode"] == "manual"
            assert last["brightness"] == 42
            assert last["channel_gains"] == (1.0, 1.0, 1.0)
        finally:
            dlg.deleteLater()

    def test_auto_recompute_runs_in_worker_thread(self, qapp, tmp_path, monkeypatch):
        """auto 模式重算走工作线程，结果回主线程刷新 QPixmap。"""
        worker_threads = []
        render_threads = []
        real_apply = gui.apply_exposure

        def _spy(img, **kwargs):
            worker_threads.append((threading.current_thread(), kwargs))
            return real_apply(img, **kwargs)

        monkeypatch.setattr(gui, "apply_exposure", _spy)

        src = tmp_path / "src.png"
        Image.new("RGB", (200, 200), (200, 200, 200)).save(src, "PNG")

        dlg = gui.PreviewDialog(None, str(src), "png",
                                str(tmp_path / "out.png"), "job_auto")
        real_set_preview = dlg._set_preview

        def _spy_set(img):
            render_threads.append(threading.current_thread())
            return real_set_preview(img)

        dlg._set_preview = _spy_set
        try:
            dlg.exposure_panel.mode_combo.setCurrentIndex(
                dlg.exposure_panel.mode_combo.findData("auto"))

            assert _wait_for(lambda: bool(render_threads), qapp=qapp), \
                "auto 重算结果没有回主线程刷新"

            assert worker_threads, "auto 模式没有调用 apply_exposure"
            assert worker_threads[0][0] is not threading.main_thread(), \
                "auto 重算没有走工作线程（在主线程里同步算了）"
            assert worker_threads[0][1]["mode"] == "auto"
            assert render_threads[0] is threading.main_thread(), \
                "auto 结果不是在主线程刷新的"
        finally:
            dlg.deleteLater()

    def test_stale_auto_result_is_discarded(self, qapp, tmp_path, monkeypatch):
        """过期结果必须丢弃：旧结果不能盖掉新结果。"""
        applied = []
        real_apply = gui.apply_exposure

        def _slow_apply(img, **kwargs):
            applied.append(kwargs)
            time.sleep(0.3)
            return real_apply(img, **kwargs)

        monkeypatch.setattr(gui, "apply_exposure", _slow_apply)

        src = tmp_path / "src.png"
        Image.new("RGB", (200, 200), (200, 200, 200)).save(src, "PNG")

        dlg = gui.PreviewDialog(None, str(src), "png",
                                str(tmp_path / "out.png"), "job_stale")
        rendered = []
        real_set_preview = dlg._set_preview

        def _spy_set(img):
            rendered.append(threading.current_thread())
            return real_set_preview(img)

        dlg._set_preview = _spy_set
        try:
            dlg.exposure_panel.mode_combo.setCurrentIndex(
                dlg.exposure_panel.mode_combo.findData("auto"))
            # 立刻再触发一次 ⇒ 第一轮结果过期
            dlg._recompute_preview()

            _wait_for(lambda: len(applied) >= 2, qapp=qapp)
            time.sleep(0.6)
            qapp.processEvents()

            # 两轮都算过，但只有最后一轮的结果被采用
            assert len(applied) >= 2, "两次重算没有都发生"
            assert len(rendered) == 1, \
                f"过期结果没有被丢弃（渲染了 {len(rendered)} 次）"
        finally:
            dlg.deleteLater()

    def test_exposure_passed_to_save_manager(self, qapp, tmp_path, tmp_config_dir,
                                             monkeypatch, no_popups):
        """曝光参数从 GUI 一路传到 save_manager。"""
        captured = {}
        real_save_single = save_manager.save_single

        def _capture(cache_path, ext, output_path, **kwargs):
            captured.update(kwargs)
            return real_save_single(cache_path, ext, output_path, **kwargs)

        monkeypatch.setattr(save_manager, "save_single", _capture)

        src = tmp_path / "src.png"
        Image.new("RGB", (100, 100), (128, 128, 128)).save(src, "PNG")

        dlg = gui.PreviewDialog(None, str(src), "png",
                                str(tmp_path / "out.png"), "job_save")
        try:
            dlg.exposure_panel.mode_combo.setCurrentIndex(
                dlg.exposure_panel.mode_combo.findData("manual"))
            dlg.exposure_panel._sliders["brightness"][0].setValue(33)
            dlg._confirm()

            assert "exposure" in captured, "save_manager 没收到 exposure 参数"
            assert captured["exposure"]["mode"] == "manual"
            assert captured["exposure"]["brightness"] == 33
        finally:
            dlg.deleteLater()

    def test_saved_file_carries_the_preview_effect(self, qapp, tmp_path,
                                                    tmp_config_dir, monkeypatch,
                                                    no_popups):
        """保存出的文件确实带上了预览里看到的曝光效果。"""
        src = tmp_path / "src.png"
        Image.new("RGB", (100, 100), (120, 120, 120)).save(src, "PNG")

        dlg = gui.PreviewDialog(None, str(src), "png",
                                str(tmp_path / "out.png"), "job_effect")
        try:
            dlg.exposure_panel.mode_combo.setCurrentIndex(
                dlg.exposure_panel.mode_combo.findData("manual"))
            dlg.exposure_panel._sliders["brightness"][0].setValue(60)
            _wait_for(lambda: True, timeout=0.3, qapp=qapp)
            dlg._confirm()
        finally:
            dlg.deleteLater()

        with Image.open(src) as original, Image.open(tmp_path / "out.png") as saved:
            assert saved.convert("RGB").getpixel((10, 10))[0] > \
                original.convert("RGB").getpixel((10, 10))[0], \
                "保存出来的文件没有带上预览里看到的曝光效果"


class TestMultiPagePreviewExposure:
    def test_exposure_passed_to_save_pages(self, qapp, tmp_path, isolated_cache,
                                           monkeypatch, no_popups):
        """
        多页预览：曝光参数传到 save_pages（文档级，作用于全部页）。

        F-5 之后落盘已挪到后台线程 ⇒ 必须等它真的跑完再断言。
        """
        captured = {}
        real_save_pages = save_manager.save_pages

        def _capture(job_id, ext, output_path, page_specs, groups, **kwargs):
            captured.update(kwargs)
            return real_save_pages(job_id, ext, output_path, page_specs, groups,
                                   **kwargs)

        monkeypatch.setattr(save_manager, "save_pages", _capture)

        job_id = "job_multi"
        cache_manager.register_job(job_id)
        buf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        buf.close()
        Image.new("RGB", (80, 80), (110, 110, 110)).save(buf.name, "PNG")
        for page in (1, 2):
            with open(buf.name, "rb") as f:
                cache_manager.write_page(job_id, page, f.read(), "png")

        dlg = gui.MultiPagePreviewDialog(None, job_id, "png",
                                         str(tmp_path / "multi.png"), 2)
        try:
            dlg.exposure_panel.mode_combo.setCurrentIndex(
                dlg.exposure_panel.mode_combo.findData("manual"))
            dlg.exposure_panel._sliders["contrast"][0].setValue(25)
            dlg._confirm()

            # 落盘在后台线程 ⇒ 等它真的发生（含回主线程的完成回调，
            # 否则回调会漏到下一个测试里弹窗）
            assert _wait_for(lambda: "exposure" in captured and dlg._save_finished,
                             qapp=qapp), "save_pages 没收到 exposure 参数"
            assert captured["exposure"]["mode"] == "manual"
            assert captured["exposure"]["contrast"] == 25
        finally:
            dlg.deleteLater()
            os.unlink(buf.name)

    def test_multi_page_auto_recompute_runs_in_worker_thread(self, qapp, tmp_path,
                                                             isolated_cache,
                                                             monkeypatch):
        """多页预览的 auto 重算同样走工作线程。"""
        worker_threads = []
        real_apply = gui.apply_exposure

        def _spy(img, **kwargs):
            worker_threads.append(threading.current_thread())
            return real_apply(img, **kwargs)

        monkeypatch.setattr(gui, "apply_exposure", _spy)

        job_id = "job_multi_auto"
        cache_manager.register_job(job_id)
        buf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        buf.close()
        Image.new("RGB", (80, 80), (200, 200, 200)).save(buf.name, "PNG")
        for page in (1, 2):
            with open(buf.name, "rb") as f:
                cache_manager.write_page(job_id, page, f.read(), "png")

        dlg = gui.MultiPagePreviewDialog(None, job_id, "png",
                                         str(tmp_path / "multi.png"), 2)
        try:
            dlg.exposure_panel.mode_combo.setCurrentIndex(
                dlg.exposure_panel.mode_combo.findData("auto"))
            assert _wait_for(lambda: bool(worker_threads), qapp=qapp), \
                "多页预览的 auto 重算没有发生"
            assert worker_threads[0] is not threading.main_thread()
        finally:
            dlg.deleteLater()
            os.unlink(buf.name)


class TestDirectSaveExposure:
    def test_direct_save_applies_exposure(self, qapp, tmp_path, tmp_config_dir,
                                           monkeypatch, no_popups):
        """
        直接保存模式（跳过预览）也要应用曝光。

        I-2 之后单页直存走 save_single（文件名不带序号），多页走 save_pages ——
        两条入口都要把 exposure 传下去，所以这里同时挂两个钩子，
        断言的是**语义**（曝光参数传到了落盘层），不是具体走哪个函数。
        """
        captured = {}
        real_save_single = save_manager.save_single
        real_save_pages = save_manager.save_pages

        def _capture_single(cache_path, ext, output_path, **kwargs):
            captured.update(kwargs)
            return real_save_single(cache_path, ext, output_path, **kwargs)

        def _capture_pages(job_id, ext, output_path, page_specs, groups, **kwargs):
            captured.update(kwargs)
            return real_save_pages(job_id, ext, output_path, page_specs, groups,
                                   **kwargs)

        monkeypatch.setattr(save_manager, "save_single", _capture_single)
        monkeypatch.setattr(save_manager, "save_pages", _capture_pages)

        import types
        src = tmp_path / "src.png"
        Image.new("RGB", (60, 60), (100, 100, 100)).save(src, "PNG")

        # ScanApp 是 QMainWindow，不能 object.__new__ —— 用只带必需属性的替身
        class _StubStatusBar:
            def showMessage(self, msg):
                pass

        stub = types.SimpleNamespace(
            selected=None,
            extra_dirs=[],
            _exposure_state={"mode": "manual", "brightness": 25},
            status_bar=_StubStatusBar(),
        )
        # 直接保存的完成/失败回主线程由这两个方法接手（16.17 / F-1）
        stub._on_direct_save_done = types.MethodType(
            gui.ScanApp._on_direct_save_done, stub)
        stub._on_direct_save_error = types.MethodType(
            gui.ScanApp._on_direct_save_error, stub)

        result = types.SimpleNamespace(
            job_id="job_direct", ext="png",
            output_path=str(tmp_path / "direct.png"),
            cached_paths=[str(src)])

        gui.ScanApp._direct_save_result(stub, result)
        # 落盘在后台线程 ⇒ 等它落地（否则断言会跑在落盘之前）
        assert _wait_for(lambda: getattr(stub, "_direct_save_finished", False),
                         qapp=qapp), "直接保存没结束"

        assert "exposure" in captured, "直接保存没带曝光参数"
        assert captured["exposure"]["brightness"] == 25
        assert captured["exposure"]["mode"] == "manual"

    def test_direct_save_without_exposure_is_unchanged(self, qapp, tmp_path,
                                                        tmp_config_dir,
                                                        monkeypatch, no_popups):
        """
        没设曝光时，直接保存的产物与不加曝光完全一致。

        I-2 之后**单页单组**的文件名不带序号（`{base}.{ext}`）——
        与预览保存、与改造前（73c18f3）完全一致；多页才是 `{base}_001.ext`。
        「无曝光 ⇒ 产物不变」这条语义与命名无关，任何方案下都必须成立。
        """
        import types

        src = tmp_path / "src.png"
        Image.new("RGB", (60, 60), (100, 100, 100)).save(src, "PNG")

        class _StubStatusBar:
            def showMessage(self, msg):
                pass

        stub = types.SimpleNamespace(
            selected=None,
            extra_dirs=[],
            _exposure_state=None,
            status_bar=_StubStatusBar(),
        )
        stub._on_direct_save_done = types.MethodType(
            gui.ScanApp._on_direct_save_done, stub)
        stub._on_direct_save_error = types.MethodType(
            gui.ScanApp._on_direct_save_error, stub)

        result = types.SimpleNamespace(
            job_id="job_direct_none", ext="png",
            output_path=str(tmp_path / "direct_none.png"),
            cached_paths=[str(src)])

        gui.ScanApp._direct_save_result(stub, result)
        assert _wait_for(lambda: getattr(stub, "_direct_save_finished", False),
                         qapp=qapp), "直接保存没结束"
        # 单页单组 ⇒ 不加序号（与预览保存同一条命名规则）
        out = tmp_path / "direct_none.png"
        assert out.exists(), f"直接保存没产出文件：{os.listdir(tmp_path)}"
        with Image.open(src) as a, Image.open(out) as b:
            assert a.convert("RGB").tobytes() == b.convert("RGB").tobytes(), \
                "无曝光时产物必须与原图逐像素一致"
