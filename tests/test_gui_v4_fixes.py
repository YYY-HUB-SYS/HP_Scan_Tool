"""
轮 4 · GUI 缺陷修复回归测试（verify-v4 的 F1 / F2 / F4 + 矩阵 16.17）—— HP Scan Tool v4.0

覆盖：
  16.17 / F1「直接保存」分支仍在主线程同步落盘 ⇒ Qt 事件循环完全停摆
        （本轮 F-5 只把**多页预览**保存挪进后台，直接保存漏了）
  F2     旋转后再裁边 ⇒ 用户的旋转被**静默清零**（产物朝向与用户意图不符）
  F4     单页任务产出 `scan.jpg`，「多页删剩 1 页」产出 `scan_001.jpg`（命名分叉）

GUI 一律 offscreen 跑（QT_QPA_PLATFORM=offscreen），不弹真实窗口、
不注入任何鼠标/键盘事件；缓存/配置/历史全部重定向到临时目录。
"""

import io
import os
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest
from PIL import Image

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
# 测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_gui_v4_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import cache_manager
    import page_meta
    import save_manager
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)


@pytest.fixture(scope="module")
def qapp(qt_app):
    """整个模块共用一个 QApplication（offscreen，不显示窗口）

    ★ 复用**会话级**实例（tests/conftest.py 的 `qt_app`）。
      自己写 `QApplication.instance() or QApplication([])` 会在进程里
      残留 QCoreApplication 时取到**错误**的实例 ⇒ 后续创建 widget
      直接崩（0xC0000409，见 tests/test_qt_app_singularity.py）。
    """
    return qt_app


class _FakeMessageBox:
    """QMessageBox 替身：记录提示调用，绝不弹窗"""

    Yes = 0x00004000
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


@pytest.fixture
def no_popups(monkeypatch):
    """所有可能弹窗的地方都换成替身（绝不弹真实窗口）"""
    fake = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake)
    monkeypatch.setattr(gui.QMessageBox, "critical", staticmethod(fake.critical))
    monkeypatch.setattr(gui.QMessageBox, "information", staticmethod(fake.information))
    monkeypatch.setattr(gui.QMessageBox, "warning", staticmethod(fake.warning))
    monkeypatch.setattr(gui.QMessageBox, "question", staticmethod(lambda *a, **k: fake.Yes))
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    return fake


class _StubApp:
    """ScanApp 的最小替身：只借用直接保存相关的方法。"""

    _on_scan_finished = gui.ScanApp._on_scan_finished
    _direct_save_result = gui.ScanApp._direct_save_result
    _on_direct_save_done = gui.ScanApp._on_direct_save_done
    _on_direct_save_error = gui.ScanApp._on_direct_save_error

    def __init__(self, direct_save=True, selected=None, extra_dirs=None,
                 exposure=None):
        self.direct_save_val = direct_save
        self.selected = selected
        self.extra_dirs = [] if extra_dirs is None else extra_dirs
        self.messages = []
        self.status_bar = types.SimpleNamespace(showMessage=self.messages.append)
        self._exposure_state = exposure
        self._direct_save_finished = False

    def _reset_scan_ui(self):
        pass

    def _show_preview(self, *a, **k):
        pass

    def _show_multi_preview(self, *a, **k):
        pass


def _wait_for(predicate, timeout=20.0, qapp=None):
    """泵事件循环直到 predicate 为真（超时返回 False）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        if qapp is not None:
            qapp.processEvents()
        time.sleep(0.005)
    return predicate()


def _make_result(tmp_path, page_count=1, ext="jpg", job_id="job_v4"):
    return types.SimpleNamespace(
        job_id=job_id,
        ext=ext,
        page_count=page_count,
        cached_paths=[str(Path(tmp_path) / f"cache_{i:03d}.{ext}")
                      for i in range(1, page_count + 1)],
        output_path=str(Path(tmp_path) / f"scan.{ext}"),
    )


def _write_page(job_id, page_num, img, ext="png"):
    """往隔离缓存里写一页，返回缓存文件路径"""
    buf = io.BytesIO()
    if ext == "png":
        img.save(buf, "PNG")
    else:
        img.save(buf, "JPEG")
    return cache_manager.write_page(job_id, page_num, buf.getvalue(), ext)


def _scan_with_border(w=100, h=120, border=30):
    """造一张带大片白边的图（auto_crop 能裁出内容）"""
    img = Image.new("RGB", (w, h), "white")
    img.paste(Image.new("RGB", (w - 2 * border, h - 2 * border), "black"),
              (border, border))
    return img


# ══════════ 16.17 / F1 直接保存不许阻塞主线程 ══════════

def test_direct_save_keeps_event_loop_alive(qapp, tmp_path, monkeypatch,
                                            no_popups):
    """
    ★ verifier F1 的判据：落盘期间 Qt 事件循环必须继续转。

    挂一个 30ms 的 QTimer，落盘（0.6s）期间它必须**触发多次**；
    修复前：整个落盘期间触发 0 次（事件循环完全停摆）。
    """
    from PySide6.QtCore import QTimer

    window = {}

    def _slow_save_pages(*a, **k):
        window["t0"] = time.time()
        time.sleep(0.6)                     # 模拟真机落盘耗时
        window["t1"] = time.time()
        return save_manager.SaveResult([str(Path(tmp_path) / "scan_001.jpg")])

    monkeypatch.setattr(save_manager, "save_pages", _slow_save_pages)

    ticks = []
    timer = QTimer()
    timer.setInterval(30)
    timer.timeout.connect(lambda: ticks.append(time.time()))
    timer.start()

    app = _StubApp(direct_save=True)
    result = _make_result(tmp_path, page_count=3)
    app._on_scan_finished(result.job_id, result)
    assert _wait_for(lambda: app._direct_save_finished, qapp=qapp), "直接保存没结束"
    timer.stop()
    qapp.processEvents()

    t0, t1 = window.get("t0"), window.get("t1")
    assert t0 and t1, "落盘没发生"
    during = [t for t in ticks if t0 <= t <= t1]
    assert len(during) >= 5, (
        f"落盘期间事件循环只转了 {len(during)} 次（30ms 的 QTimer）"
        f" ⇒ 界面在落盘全程是卡死的（修复前是 0 次）")

    # 最长的一次停摆必须远小于整个落盘耗时
    seq = sorted(during)
    gaps = [b - a for a, b in zip(seq, seq[1:])]
    max_gap = max(gaps) if gaps else (t1 - t0)
    assert max_gap < 0.3, f"事件循环最长停摆 {max_gap:.3f}s（落盘共 {t1 - t0:.3f}s）"


def test_direct_save_runs_off_main_thread(qapp, tmp_path, isolated_cache,
                                          monkeypatch, no_popups):
    """落盘必须在后台线程；完成/失败回调必须回主线程"""
    save_threads = []
    callback_threads = []
    real_save = save_manager.save_pages

    def _spy(*a, **k):
        save_threads.append(threading.current_thread())
        return real_save(*a, **k)

    monkeypatch.setattr(save_manager, "save_pages", _spy)

    job_id = "job_v4_thread"
    cached = [_write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)),
                          ext="jpg") for i in (1, 2, 3)]

    app = _StubApp(direct_save=True)
    real_done = app._on_direct_save_done

    def _spy_done(*args, **kwargs):
        callback_threads.append(threading.current_thread())
        return real_done(*args, **kwargs)

    app._on_direct_save_done = _spy_done
    result = _make_result(tmp_path, page_count=3, ext="jpg", job_id=job_id)
    result.cached_paths = cached
    app._on_scan_finished(result.job_id, result)
    assert _wait_for(lambda: app._direct_save_finished, qapp=qapp), "直接保存没结束"

    assert save_threads, "没调用 save_pages"
    assert save_threads[0] is not threading.main_thread(), \
        "落盘还在主线程（会把界面卡死）"
    assert callback_threads, "没收到完成回调"
    assert callback_threads[0] is threading.main_thread(), "完成回调没回主线程"


def test_direct_save_success_reports_file_count(qapp, tmp_path, monkeypatch,
                                                no_popups):
    """成功路径：产出文件 + 提示里含总数"""
    files = [str(Path(tmp_path) / f"scan_{i:03d}.jpg") for i in range(1, 4)]
    monkeypatch.setattr(
        save_manager, "save_pages",
        lambda *a, **k: save_manager.SaveResult(files, failed_pages=0))

    app = _StubApp(direct_save=True)
    result = _make_result(tmp_path, page_count=3)
    app._on_scan_finished(result.job_id, result)
    assert _wait_for(lambda: app._direct_save_finished, qapp=qapp), "直接保存没结束"

    assert no_popups.information_calls, "保存完必须有提示"
    text = no_popups.information_calls[0][1]
    assert "共 3 个文件" in text, f"提示没报文件总数: {text!r}"
    assert any("已保存" in m for m in app.messages), \
        f"状态栏没报保存结果: {app.messages}"


def test_direct_save_failure_is_reported(qapp, tmp_path, monkeypatch, no_popups):
    """失败路径：异常被捕获并提示（不能静默）"""

    def _boom(*a, **k):
        raise RuntimeError("磁盘写入失败")

    monkeypatch.setattr(save_manager, "save_pages", _boom)

    app = _StubApp(direct_save=True)
    result = _make_result(tmp_path, page_count=3)
    app._on_scan_finished(result.job_id, result)
    assert _wait_for(lambda: app._direct_save_finished, qapp=qapp), "直接保存没结束"

    assert no_popups.critical_calls, "保存失败必须弹错误提示"
    assert not no_popups.information_calls, "失败时不该弹成功提示"
    assert "磁盘写入失败" in no_popups.critical_calls[0][1]


# ══════════ F2 裁边不许清掉用户的旋转 ══════════

def test_rotate_rect_transform_matches_pil():
    """坐标变换辅助函数：结果必须与 PIL 的实际旋转对得上"""
    src = Image.new("RGB", (100, 120), "white")
    rect = (25, 25, 75, 95)                 # 裁边框（原图坐标系）
    for angle in (0, 90, 180, 270):
        rotated = src.rotate(angle, expand=True)
        new = gui._rotate_rect(rect, angle, src.size)
        assert new is not None
        # 变换后的框必须落在旋转后的画布里
        assert 0 <= new[0] < new[2] <= rotated.width
        assert 0 <= new[1] < new[3] <= rotated.height
        # 框的尺寸 = 裁剪块的尺寸（90/270 对调）
        rw, rh = rect[2] - rect[0], rect[3] - rect[1]
        if angle in (90, 270):
            assert (new[2] - new[0], new[3] - new[1]) == (rh, rw)
        else:
            assert (new[2] - new[0], new[3] - new[1]) == (rw, rh)


@pytest.mark.parametrize("angle,expected", [
    (90, (70, 50)),        # 裁后 50×70，旋转 90 ⇒ 70×50
    (180, (50, 70)),
    (270, (70, 50)),
])
def test_single_page_crop_keeps_rotation(qapp, tmp_path, isolated_cache,
                                         angle, expected):
    """
    单页预览：先旋转再裁边 ⇒ 旋转**不能**被清零，产物尺寸 = 旋转后的裁边结果。
    """
    job_id = f"job_v4_rot_{angle}"
    cache = _write_page(job_id, 1, _scan_with_border(), ext="png")
    out = tmp_path / "out.png"

    dlg = gui.PreviewDialog(None, cache, "png", str(out), job_id)
    try:
        for _ in range(angle // 90):
            dlg._rotate_right()
        assert dlg._rotation == angle, "测试前置条件不成立"
        dlg._auto_crop()
        assert dlg._rotation == angle, \
            f"裁边把用户的旋转清零了（{angle}° ⇒ {dlg._rotation}°）"
        dlg._confirm()
    finally:
        dlg.deleteLater()

    assert out.exists(), "没产出文件"
    with Image.open(out) as im:
        assert im.size == expected, \
            f"旋转 {angle}° + 裁边的产物尺寸不对: {im.size} != {expected}"


@pytest.mark.parametrize("angle,expected", [
    (90, (70, 50)),
    (180, (50, 70)),
    (270, (70, 50)),
])
def test_single_page_crop_page_meta_matches_actual(qapp, tmp_path,
                                                   isolated_cache, angle,
                                                   expected):
    """单页：page_meta 的 manual_rotation / 宽高 / crop_rect 必须与实际一致"""
    job_id = f"job_v4_meta_{angle}"
    cache = _write_page(job_id, 1, _scan_with_border(), ext="png")
    out = tmp_path / "out.png"

    dlg = gui.PreviewDialog(None, cache, "png", str(out), job_id)
    try:
        for _ in range(angle // 90):
            dlg._rotate_right()
        dlg._auto_crop()
        # ★ page_meta 必须在**保存前**读：save_manager 落盘成功后会清掉
        #   该 job 的 page_meta（save_manager._remove_page_meta）
        meta = page_meta.PageMetaStore(cache_manager._cache_root()).get_job(job_id)["1"]
        dlg._confirm()
    finally:
        dlg.deleteLater()

    assert out.exists(), "没产出文件"
    with Image.open(out) as im:
        assert im.size == expected, \
            f"旋转 {angle}° + 裁边的产物尺寸不对: {im.size} != {expected}"
    assert meta["manual_rotation"] == angle, \
        f"manual_rotation 没落盘: {meta.get('manual_rotation')}"
    assert (meta["width"], meta["height"]) == expected, \
        f"page_meta 尺寸与产物不符: {meta['width']}x{meta['height']} != {expected}"
    assert meta["crop_rect"], "crop_rect 没写"
    left, top, right, bottom = meta["crop_rect"]
    assert (right - left, bottom - top) == expected, \
        f"crop_rect 尺寸与产物不符: {(right - left, bottom - top)} != {expected}"
    assert meta["cache_path"] == cache, "cache_path 必须仍指向原始扫描件"


@pytest.mark.parametrize("angle,expected", [
    (90, (70, 50)),
    (180, (50, 70)),
    (270, (70, 50)),
])
def test_multi_page_crop_keeps_rotation(qapp, tmp_path, isolated_cache,
                                        tmp_config_dir, monkeypatch, no_popups,
                                        angle, expected):
    """多页预览：同样不许把用户的旋转清零"""
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    job_id = f"job_v4_multi_{angle}"
    _write_page(job_id, 1, _scan_with_border(), ext="png")
    _write_page(job_id, 2, Image.new("RGB", (100, 120), (200, 200, 200)),
                ext="png")

    dlg = gui.MultiPagePreviewDialog(None, job_id, "png",
                                     str(tmp_path / "multi.png"), 2)
    try:
        dlg.selected_idx = 0
        for _ in range(angle // 90):
            dlg._rotate_right()
        assert dlg._get_rotation(1) == angle, "测试前置条件不成立"
        dlg._auto_crop()
        assert dlg._get_rotation(1) == angle, \
            f"裁边把该页的旋转清零了（{angle}° ⇒ {dlg._get_rotation(1)}°）"
        dlg._confirm()
        assert _wait_for(lambda: dlg._save_finished, qapp=qapp), "保存没结束"
    finally:
        dlg.deleteLater()

    out = tmp_path / "multi_001.png"
    assert out.exists(), f"没产出文件: {sorted(p.name for p in tmp_path.glob('*.png'))}"
    with Image.open(out) as im:
        assert im.size == expected, \
            f"旋转 {angle}° + 裁边的产物尺寸不对: {im.size} != {expected}"


@pytest.mark.parametrize("angle,expected", [
    (90, (70, 50)),
    (180, (50, 70)),
    (270, (70, 50)),
])
def test_multi_page_crop_page_meta_matches_actual(qapp, tmp_path,
                                                  isolated_cache,
                                                  tmp_config_dir, monkeypatch,
                                                  no_popups, angle, expected):
    """多页：page_meta 的 manual_rotation / 宽高 / crop_rect 必须与实际一致"""
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    job_id = f"job_v4_multi_meta_{angle}"
    _write_page(job_id, 1, _scan_with_border(), ext="png")
    _write_page(job_id, 2, Image.new("RGB", (100, 120), (200, 200, 200)),
                ext="png")

    dlg = gui.MultiPagePreviewDialog(None, job_id, "png",
                                     str(tmp_path / "multi.png"), 2)
    try:
        dlg.selected_idx = 0
        for _ in range(angle // 90):
            dlg._rotate_right()
        dlg._auto_crop()
        # ★ page_meta 在保存前读（落盘成功后 save_manager 会清掉它）
        meta = page_meta.PageMetaStore(cache_manager._cache_root()).get_job(job_id)["1"]
        dlg._confirm()
        assert _wait_for(lambda: dlg._save_finished, qapp=qapp), "保存没结束"
    finally:
        dlg.deleteLater()

    out = tmp_path / "multi_001.png"
    assert out.exists(), f"没产出文件: {sorted(p.name for p in tmp_path.glob('*.png'))}"
    with Image.open(out) as im:
        assert im.size == expected, \
            f"旋转 {angle}° + 裁边的产物尺寸不对: {im.size} != {expected}"
    assert meta["manual_rotation"] == angle, \
        f"manual_rotation 没落盘: {meta.get('manual_rotation')}"
    assert (meta["width"], meta["height"]) == expected, \
        f"page_meta 尺寸与产物不符: {meta['width']}x{meta['height']} != {expected}"
    left, top, right, bottom = meta["crop_rect"]
    assert (right - left, bottom - top) == expected, \
        f"crop_rect 尺寸与产物不符: {(right - left, bottom - top)} != {expected}"


def test_crop_without_rotation_keeps_legacy_geometry(qapp, tmp_path,
                                                      isolated_cache):
    """不旋转时裁边的几何与改造前一致（50×70，crop_rect 落在原图坐标系）"""
    job_id = "job_v4_norot"
    cache = _write_page(job_id, 1, _scan_with_border(), ext="png")
    out = tmp_path / "out.png"

    dlg = gui.PreviewDialog(None, cache, "png", str(out), job_id)
    try:
        dlg._auto_crop()
        # ★ page_meta 在保存前读（落盘成功后 save_manager 会清掉它）
        meta = page_meta.PageMetaStore(cache_manager._cache_root()).get_job(job_id)["1"]
        dlg._confirm()
    finally:
        dlg.deleteLater()

    with Image.open(out) as im:
        assert im.size == (50, 70), f"不旋转时产物尺寸变了: {im.size}"
    assert meta["manual_rotation"] == 0
    assert (meta["width"], meta["height"]) == (50, 70)
    left, top, right, bottom = meta["crop_rect"]
    assert (left, top, right, bottom) == (25, 25, 75, 95), \
        f"不旋转时 crop_rect 变了: {meta['crop_rect']}"


# ══════════ F4 单页任务的命名必须两边一致 ══════════

def test_multi_page_left_with_one_page_uses_single_page_name(qapp, tmp_path,
                                                              isolated_cache,
                                                              tmp_config_dir,
                                                              monkeypatch,
                                                              no_popups):
    """
    「多页删剩 1 页」与「单页任务」必须产出**同名**文件。

    语义选「单页任务」：单页单组 ⇒ `{base}.{ext}`（不加序号）。
    """
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)

    # ① 单页任务（单页预览框）
    single_dir = tmp_path / "single"
    single_dir.mkdir()
    cache = _write_page("job_v4_single", 1,
                        Image.new("RGB", (60, 80), (200, 220, 240)), ext="jpg")
    dlg = gui.PreviewDialog(None, cache, "jpg", str(single_dir / "scan.jpg"),
                            "job_v4_single")
    try:
        dlg._confirm()
    finally:
        dlg.deleteLater()
    single_names = sorted(p.name for p in single_dir.glob("scan*.jpg"))
    assert single_names == ["scan.jpg"], f"单页任务命名变了: {single_names}"

    # ② 多页预览框删到只剩 1 页
    multi_dir = tmp_path / "multi"
    multi_dir.mkdir()
    job_id = "job_v4_multi_one"
    _write_page(job_id, 1, Image.new("RGB", (60, 80), (200, 220, 240)), ext="jpg")
    _write_page(job_id, 2, Image.new("RGB", (60, 80), (200, 220, 240)), ext="jpg")
    mdlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                      str(multi_dir / "scan.jpg"), 2)
    try:
        mdlg.selected_idx = 1
        mdlg._delete_current_page()
        assert len(mdlg.pages) == 1, "删页没生效"
        mdlg._confirm()
        assert _wait_for(lambda: mdlg._save_finished, qapp=qapp), "保存没结束"
    finally:
        mdlg.deleteLater()

    multi_names = sorted(p.name for p in multi_dir.glob("scan*.jpg"))
    assert multi_names == single_names, \
        f"两条路径命名不一致: 单页={single_names} 多页删剩1页={multi_names}"


def test_multi_page_still_uses_sequence_for_several_pages(qapp, tmp_path,
                                                          isolated_cache,
                                                          tmp_config_dir,
                                                          monkeypatch,
                                                          no_popups):
    """多页（>1）仍用 `{base}_001` 序号（不能被 F4 的修复带偏）"""
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    job_id = "job_v4_multi_sev"
    for i in (1, 2):
        _write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)),
                    ext="jpg")

    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                     str(tmp_path / "scan.jpg"), 2)
    try:
        dlg._confirm()
        assert _wait_for(lambda: dlg._save_finished, qapp=qapp), "保存没结束"
    finally:
        dlg.deleteLater()

    names = sorted(p.name for p in tmp_path.glob("scan*.jpg"))
    assert names == ["scan_001.jpg", "scan_002.jpg"], f"多页命名不对: {names}"


# ══════════ V1-N-2 GUI 不许谎报「配置已保存」 ══════════

class _CfgStubApp:
    """ScanApp 的最小替身：只借用配置保存相关的方法。"""

    _save_cfg = gui.ScanApp._save_cfg
    _apply_cache_settings = gui.ScanApp._apply_cache_settings
    _apply_custom_name = gui.ScanApp._apply_custom_name

    def __init__(self, coordinator=None):
        self.coordinator = (coordinator if coordinator is not None
                            else types.SimpleNamespace(cfg={}))
        self.messages = []
        self.status_bar = types.SimpleNamespace(showMessage=self.messages.append)
        self.cfg = {"resolution": 300}

    def _rebuild_cards(self):
        pass


def test_cache_settings_failure_does_not_claim_saved(qapp, monkeypatch,
                                                     no_popups):
    """写失败 ⇒ 提示里**不能**出现"已保存"（V1-N-2）"""
    monkeypatch.setattr(gui.cache_manager, "save_cache_config", lambda cfg: False)
    app = _CfgStubApp()

    ok = app._apply_cache_settings({}, 256, 90, 200)

    assert ok is False, "写失败必须如实返回 False"
    assert not any("已保存" in m for m in app.messages), \
        f"写失败却提示已保存: {app.messages}"
    assert any("保存失败" in m for m in app.messages), \
        f"没有失败提示: {app.messages}"
    assert no_popups.warning_calls, "写失败必须弹提示"
    assert not any("已保存" in text for _t, text in no_popups.warning_calls)


def test_cache_settings_success_claims_saved(qapp, monkeypatch, no_popups):
    """写成功 ⇒ 提示含"已保存"（V1-N-2）"""
    monkeypatch.setattr(gui.cache_manager, "save_cache_config", lambda cfg: True)
    app = _CfgStubApp()

    ok = app._apply_cache_settings({}, 256, 90, 200)

    assert ok is True
    assert any("已保存" in m for m in app.messages), \
        f"写成功却没说已保存: {app.messages}"
    assert not no_popups.warning_calls, "写成功不该弹失败提示"


def test_save_cfg_failure_is_reported(qapp, monkeypatch):
    """通用配置写入：失败必须如实上报（不能静默吞掉）"""
    monkeypatch.setattr(gui, "save_config", lambda cfg: False)
    app = _CfgStubApp()

    assert app._save_cfg() is False
    assert any("保存失败" in m for m in app.messages), \
        f"配置写失败没有提示: {app.messages}"

    monkeypatch.setattr(gui, "save_config", lambda cfg: True)
    assert app._save_cfg() is True


def test_rename_failure_does_not_claim_renamed(qapp, monkeypatch, no_popups):
    """改名写失败 ⇒ 不能说"已重命名"（V1-N-2）"""
    monkeypatch.setattr(gui, "save_config", lambda cfg: False)
    app = _CfgStubApp()
    scanner = types.SimpleNamespace(ip="192.168.1.20", custom_name="")

    ok = app._apply_custom_name(scanner, "新名字")

    assert ok is False
    assert not any("已重命名为" in m for m in app.messages), \
        f"写失败却说已重命名: {app.messages}"
    assert no_popups.warning_calls, "写失败必须弹提示"


def test_rename_success_claims_renamed(qapp, monkeypatch, no_popups):
    """改名写成功 ⇒ 说"已重命名"，且昵称真的进了配置"""
    saved = {}
    monkeypatch.setattr(gui, "save_config", lambda cfg: saved.update(cfg) or True)
    app = _CfgStubApp()
    scanner = types.SimpleNamespace(ip="192.168.1.20", custom_name="")

    ok = app._apply_custom_name(scanner, "新名字")

    assert ok is True
    assert any("已重命名为" in m for m in app.messages), app.messages
    assert saved.get("nicknames", {}).get("192.168.1.20") == "新名字"


# ══════════ V3-F-4 WIA 路径必须可取消 ══════════

class _StatusBar:
    """真状态栏（要支持 addPermanentWidget）+ 消息记录"""

    def __init__(self):
        from PySide6.QtWidgets import QStatusBar
        self._bar = QStatusBar()
        self.messages = []

    def showMessage(self, msg, timeout=0):
        self.messages.append(msg)
        self._bar.showMessage(msg, timeout)

    def addPermanentWidget(self, widget):
        self._bar.addPermanentWidget(widget)

    def removeWidget(self, widget):
        self._bar.removeWidget(widget)


class _WiaStubApp:
    """WIA 路径的最小替身：只借用 _start_wia_scan 及其依赖。"""

    _start_wia_scan = gui.ScanApp._start_wia_scan
    _on_wia_cancelled = gui.ScanApp._on_wia_cancelled
    _register_cancel_event = gui.ScanApp._register_cancel_event
    _clear_cancel_event = gui.ScanApp._clear_cancel_event
    _cancel_scan = gui.ScanApp._cancel_scan

    def __init__(self, coordinator, tmp_path):
        from PySide6.QtWidgets import QLineEdit, QPushButton
        self.coordinator = coordinator
        self.messages = []
        self.errors = []
        self.preview_calls = []
        self.status_bar = _StatusBar()
        self.scan_btn = QPushButton("扫描")
        self.dir_entry = QLineEdit(str(tmp_path))
        self.output_dir = str(tmp_path)
        self.resolution_val = 300
        self.output_format_val = "jpg"
        self._wia_cancel_event = None
        self._current_job_id = None

    def _reset_exposure_state(self):
        pass

    def _reset_scan_ui(self):
        pass

    def _post_ui(self, fn):
        gui._post_to_ui(fn)

    def _show_preview(self, *a, **k):
        self.preview_calls.append((a, k))

    def _scan_error(self, msg):
        self.errors.append(msg)

    def _release_job(self, job_id):
        pass


def test_wia_cancel_event_is_the_one_registered_by_coordinator(qapp, tmp_path,
                                                               monkeypatch):
    """
    WIA 路径：引擎收到的 cancel_event 必须**就是协调器登记的那一个**（is 相同）。

    没有它 ⇒ coordinator.cancel_scan(job_id) 停不掉 WIA 扫描（假取消）。
    """
    import wia_engine
    from escl_engine import ScanCancelled

    captured = {}
    started = threading.Event()

    def _fake_wia_scan(**kwargs):
        captured["cancel_event"] = kwargs.get("cancel_event")
        started.set()
        ev = kwargs["cancel_event"]
        deadline = time.time() + 10
        while not ev.is_set() and time.time() < deadline:
            time.sleep(0.01)
        raise ScanCancelled("WIA 扫描已取消")

    monkeypatch.setattr(wia_engine, "wia_scan", _fake_wia_scan)

    import scan_coordinator
    coordinator = scan_coordinator.ScanCoordinator()
    app = _WiaStubApp(coordinator, tmp_path)

    app._start_wia_scan({"id": "usb-1"})
    assert started.wait(5), "WIA 引擎没被调用"

    job_id = app._current_job_id
    registered = coordinator._cancel_events.get(job_id)
    assert registered is not None, "取消信号没登记进协调器"
    assert captured["cancel_event"] is registered, \
        "引擎收到的不是协调器登记的那一个 event"

    # 取消：协调器 set 的正是引擎手里那个
    coordinator.cancel_scan(job_id)
    assert captured["cancel_event"].is_set(), "取消后引擎没收到取消信号"


def test_wia_cancel_reports_cancelled_not_failed(qapp, tmp_path, monkeypatch):
    """取消后必须发 scan_cancelled（**不是** scan_failed）"""
    import wia_engine
    from escl_engine import ScanCancelled

    def _fake_wia_scan(**kwargs):
        ev = kwargs["cancel_event"]
        deadline = time.time() + 10
        while not ev.is_set() and time.time() < deadline:
            time.sleep(0.01)
        raise ScanCancelled("WIA 扫描已取消")

    monkeypatch.setattr(wia_engine, "wia_scan", _fake_wia_scan)

    import scan_coordinator
    coordinator = scan_coordinator.ScanCoordinator()
    cancelled = []
    failed = []
    coordinator.scan_cancelled.connect(lambda jid: cancelled.append(jid))
    coordinator.scan_failed.connect(lambda jid, err: failed.append(jid))

    app = _WiaStubApp(coordinator, tmp_path)
    app._start_wia_scan({"id": "usb-1"})
    job_id = app._current_job_id
    coordinator.cancel_scan(job_id)

    assert _wait_for(lambda: cancelled, qapp=qapp), "取消后没发 scan_cancelled"
    assert not failed, f"取消被报成了失败: {failed}"
    assert not app.errors, f"取消走了错误提示路径: {app.errors}"
    assert cancelled[0] == job_id

