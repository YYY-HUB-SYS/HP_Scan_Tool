"""
GUI 并发落盘 / 退出等待的回归测试 —— HP Scan Tool v4.0

覆盖复审（`verify-recheck-latest.md` §3）的两条新增风险：

  A1-R1【中】后台化引入新竞态：
         ① 落盘期间扫描按钮被重新启用 ⇒ 用户能再扫一份 ⇒ **两个 save_pages 并发重叠**；
         ② `_direct_save_finished` 是**单个布尔标志** ⇒ 先结束的那次会提前置 True；
         ③ 协调器文件名精度**只到秒** ⇒ 同秒完成的两批**互相覆盖**。
  A1-R2【低】落盘线程是 **daemon**，`closeEvent` 不等待 ⇒ 落盘中途退出会留下**截断 PDF**。

判据（一次验完）：
  - 落盘在途 ⇒ 扫描按钮**禁用**；
  - 两次落盘重叠 ⇒ **两个产物都在**且**内容互不覆盖**；
  - "全部完成"在**两个**落盘都结束前必须是 False；
  - 落盘途中关窗 ⇒ 产物**要么完整、要么不存在**（不能是截断的）。

GUI 一律 offscreen 跑（QT_QPA_PLATFORM=offscreen），不弹真实窗口、
不注入任何鼠标/键盘事件、不抢焦点；缓存/配置/历史全部重定向到临时目录。
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
_TMP_HOME = tempfile.mkdtemp(prefix="hp_gui_race_home_")
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
def qapp():
    """整个模块共用一个 QApplication（offscreen，不显示窗口）"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


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
    """ScanApp 的最小替身：只借用直接保存相关方法 + 一个**真的**扫描按钮。"""

    _on_scan_finished = gui.ScanApp._on_scan_finished
    _direct_save_result = gui.ScanApp._direct_save_result
    _on_direct_save_done = gui.ScanApp._on_direct_save_done
    _on_direct_save_error = gui.ScanApp._on_direct_save_error

    def __init__(self, direct_save=True, selected=None, extra_dirs=None,
                 exposure=None):
        from PySide6.QtWidgets import QPushButton
        self.direct_save_val = direct_save
        self.selected = selected
        self.extra_dirs = [] if extra_dirs is None else extra_dirs
        self.messages = []
        self.status_bar = types.SimpleNamespace(showMessage=self.messages.append)
        self._exposure_state = exposure
        self._direct_save_finished = False
        self.scan_btn = QPushButton("扫描")

    def _reset_scan_ui(self):
        pass

    def _show_preview(self, *a, **k):
        pass

    def _show_multi_preview(self, *a, **k):
        pass


class _FakeCloseEvent:
    """closeEvent 的替身：记录"这次关闭被接受还是被拒绝"。"""

    def __init__(self):
        self.accepted = True

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


def _close_event():
    """真 QCloseEvent（`super().closeEvent()` 只认真事件）"""
    from PySide6.QtGui import QCloseEvent
    return QCloseEvent()


def _wait_for(predicate, timeout=30.0, qapp=None):
    """泵事件循环直到 predicate 为真（超时返回 False）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        if qapp is not None:
            qapp.processEvents()
        time.sleep(0.005)
    return bool(predicate())


def _write_page(job_id, page_num, img, ext="jpg"):
    """往隔离缓存里写一页，返回缓存文件路径"""
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return cache_manager.write_page(job_id, page_num, buf.getvalue(), ext)


def _make_result(job_id, cached_paths, ext="jpg", output_path=None):
    return types.SimpleNamespace(
        job_id=job_id,
        ext=ext,
        page_count=len(cached_paths),
        cached_paths=list(cached_paths),
        output_path=output_path or "",
    )


def _make_window(monkeypatch, out_dir, direct_save=True):
    """真 ScanApp（offscreen）+ 输出目录重定向（**绝不碰真机**）"""
    # ★ 拦住"启动流程"里的 _finish_loading（它会 show() + 自动恢复会话）
    monkeypatch.setattr(gui.ScanApp, "_finish_loading", lambda self: None)
    win = gui.ScanApp()
    win.dir_entry.setText(str(out_dir))
    win.resolution_val = 72                       # 小画布 ⇒ 测试快
    win.output_format_val = "pdf"
    win.format_combo.setCurrentText("pdf")
    win.direct_save_val = direct_save
    win.selected = gui.ScannerInfo(name="测试扫描仪", ip="192.168.1.9",
                                   model="HP Test")
    return win


# ══════════ 任务 1：落盘在途 ⇒ 扫描按钮禁用 / 两个产物都在 ══════════

def test_scan_button_stays_disabled_while_saving(qapp, tmp_path, isolated_cache,
                                                  monkeypatch, no_popups):
    """
    ★ A1-R1 ①：落盘**进行中**扫描按钮必须保持禁用。

    修复前：`_on_scan_finished` 一进来就 `_reset_scan_ui()`（无条件
    `setEnabled(True)`）⇒ 落盘还没完按钮就能点 ⇒ 用户能再扫一份。
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    started = threading.Event()
    release = threading.Event()
    real_save_single = save_manager.save_single

    def _blocked_save(cache_path, *a, **k):
        started.set()
        release.wait(timeout=20)
        return real_save_single(cache_path, *a, **k)

    monkeypatch.setattr(save_manager, "save_single", _blocked_save)

    win = _make_window(monkeypatch, out_dir)
    try:
        page = _write_page("job_btn", 1, Image.new("RGB", (40, 50), (10, 20, 30)))
        result = _make_result("job_btn", [page],
                               output_path=str(out_dir / "HP_Scan_btn.jpg"))
        win._on_scan_finished("job_btn", result)

        assert started.wait(timeout=10), "落盘没起来"
        qapp.processEvents()
        assert not win.scan_btn.isEnabled(), \
            "落盘期间扫描按钮被重新启用了（用户能再点一次扫描）"
        assert win.scan_btn.text() == "保存中...", \
            f"落盘期间按钮文案没提示保存中: {win.scan_btn.text()!r}"

        release.set()
        assert _wait_for(lambda: gui._pending_save_count(win) == 0, qapp=qapp), \
            "落盘没结束"
        assert win.scan_btn.isEnabled(), "落盘结束后扫描按钮没恢复"
        assert win.scan_btn.text() == "扫描"
    finally:
        release.set()
        win.close()


def test_overlapping_saves_keep_both_products(qapp, tmp_path, isolated_cache,
                                               monkeypatch, no_popups):
    """
    ★ A1-R1 ①②③ 一次验完：
      ① 两个落盘**真的重叠**（复审实测到的那种重叠）；
      ② 两个产物**都在**且**内容互不覆盖**（文件名撞车也不能覆盖）；
      ③ "全部完成"在**两个**落盘都结束前必须是 False。
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # ★ 两批内容不同（红/蓝），文件名**完全相同** ——
    #   协调器精度只到秒 ⇒ 同秒完成的两批必然算出同一个名字。
    same_output = str(out_dir / "HP_Scan_20261021_034200.jpg")
    fast_page = _write_page("job_fast", 1, Image.new("RGB", (40, 50), (255, 0, 0)))
    slow_page = _write_page("job_slow", 1, Image.new("RGB", (40, 50), (0, 0, 255)))

    real_save_single = save_manager.save_single
    barrier = threading.Barrier(2, timeout=20)
    spans = []

    def _slow_save_single(cache_path, *a, **k):
        # 两个保存都进来了才放行 ⇒ **必然重叠**（复现复审实测的场景）
        barrier.wait()
        t0 = time.monotonic()
        time.sleep(0.3 if "job_fast" in str(cache_path) else 1.2)
        out = real_save_single(cache_path, *a, **k)
        spans.append((t0, time.monotonic(), str(cache_path)))
        return out

    monkeypatch.setattr(save_manager, "save_single", _slow_save_single)

    app = _StubApp(direct_save=True)
    app._on_scan_finished(
        "job_fast", _make_result("job_fast", [fast_page], output_path=same_output))
    app._on_scan_finished(
        "job_slow", _make_result("job_slow", [slow_page], output_path=same_output))

    assert _wait_for(lambda: gui._pending_save_count(app) == 2, qapp=qapp), \
        "两次落盘没有同时在飞"

    # ① 落盘在途 ⇒ 扫描按钮禁用
    assert not app.scan_btn.isEnabled(), "落盘期间扫描按钮被重新启用了"

    # 第一个结束、第二个还在跑 ⇒ "全部完成"必须还是 False
    assert _wait_for(lambda: gui._pending_save_count(app) == 1, qapp=qapp), \
        "第一个落盘没结束"
    assert app._direct_save_finished is False, \
        "还有一个落盘在跑，'全部完成'却已经置 True（单个布尔标志的老毛病）"
    assert not app.scan_btn.isEnabled(), "还有落盘在跑，扫描按钮却能点了"

    # 第二个也结束
    assert _wait_for(lambda: gui._pending_save_count(app) == 0, qapp=qapp), \
        "第二个落盘没结束"
    assert app._direct_save_finished is True, "两个落盘都结束后标志仍不是 True"
    assert app.scan_btn.isEnabled(), "落盘都结束了扫描按钮还禁着"

    # 真的重叠过（时间区间相交）
    assert len(spans) == 2, f"两次落盘没都跑起来: {spans}"
    (s0, e0, _), (s1, e1, _) = sorted(spans)
    assert s0 < e1 and s1 < e0, f"两次落盘没有重叠: {spans}"

    # ② 两个产物都在、内容互不覆盖
    files = sorted(os.listdir(out_dir))
    assert len(files) == 2, f"两次并发落盘只留下 {files}（应当是两个产物）"
    colors = {}
    for name in files:
        with Image.open(out_dir / name) as im:
            colors[name] = im.convert("RGB").getpixel((5, 5))
    reds = [c for c in colors.values() if c[0] > 200 and c[2] < 60]
    blues = [c for c in colors.values() if c[2] > 200 and c[0] < 60]
    assert len(reds) == 1 and len(blues) == 1, \
        f"两个产物内容被互相覆盖了: {colors}"


def test_unique_output_path_never_overwrites(tmp_path):
    """
    ★ A1-R1 ③：同一秒完成的两批 ⇒ 输出路径必须唯一（不能互相覆盖）。

    协调器给的文件名精度只到秒（`scan_coordinator.py:727`）⇒ 同秒必撞名。
    协调器不在本轮修改范围内 ⇒ GUI 侧兜底。
    """
    same = str(tmp_path / "HP_Scan_20261021_034200.pdf")
    first = gui._unique_output_path(same)
    second = gui._unique_output_path(same)
    assert first != second, "两次占用拿到了同一个路径"
    assert os.path.dirname(first) == os.path.dirname(second)
    # 第二个不能是第一个的"同前缀文件"（否则第一个的产物会被覆盖）
    assert not os.path.basename(second).startswith(
        os.path.basename(first).rsplit(".", 1)[0] + ".")

    # 磁盘上已经有产物时 ⇒ 也不能再占用同一个名字
    Path(first).write_bytes(b"%PDF-1.4 fake")
    third = gui._unique_output_path(same)
    assert third != first, "磁盘上已有产物时还占用了同一个名字"


def test_background_save_thread_is_not_daemon(qapp, tmp_path, isolated_cache,
                                              monkeypatch, no_popups):
    """
    ★ A1-R2：落盘线程**不能是 daemon**。

    daemon 线程在进程退出时被强杀 ⇒ 写到一半的 PDF 留在磁盘上。
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    seen = {}
    real_save_single = save_manager.save_single

    def _spy_save(cache_path, *a, **k):
        seen["thread"] = threading.current_thread()
        return real_save_single(cache_path, *a, **k)

    monkeypatch.setattr(save_manager, "save_single", _spy_save)

    app = _StubApp(direct_save=True)
    page = _write_page("job_daemon", 1, Image.new("RGB", (40, 50), (1, 2, 3)))
    app._on_scan_finished(
        "job_daemon",
        _make_result("job_daemon", [page],
                     output_path=str(out_dir / "HP_Scan_daemon.jpg")))
    assert _wait_for(lambda: gui._pending_save_count(app) == 0, qapp=qapp), \
        "落盘没结束"

    assert seen.get("thread") is not None, "没抓到落盘线程"
    assert not seen["thread"].daemon, \
        "落盘线程还是 daemon ⇒ 关窗时会被强杀 ⇒ 留下截断文件"


# ══════════ 任务 2：关窗时等待在途落盘 ══════════

def test_close_waits_for_inflight_save_product_is_complete(
        qapp, tmp_path, isolated_cache, monkeypatch, no_popups):
    """
    ★ A1-R2：落盘**途中**关窗 ⇒ 产物必须**完整**（不能是截断的 PDF）。

    判据：closeEvent 返回后，PDF 能被 pypdf 打开、页数正确、结尾有 %%EOF。
    """
    from pypdf import PdfReader

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    started = threading.Event()
    real_save_pages = save_manager.save_pages

    def _slow_save_pages(*a, **k):
        started.set()
        time.sleep(0.8)                    # 模拟真机落盘耗时
        return real_save_pages(*a, **k)

    monkeypatch.setattr(save_manager, "save_pages", _slow_save_pages)

    win = _make_window(monkeypatch, out_dir)
    try:
        pages = [_write_page("job_close", i, Image.new("RGB", (60, 80), (9, 9, i * 40)),
                              ext="png") for i in (1, 2, 3)]
        out_path = str(out_dir / "HP_Scan_close.pdf")
        win._direct_save_result(
            _make_result("job_close", pages, ext="pdf", output_path=out_path))
        assert started.wait(timeout=10), "落盘没起来"
        qapp.processEvents()

        # 落盘**进行中** ⇒ 触发关闭
        event = _close_event()
        win.closeEvent(event)

        assert event.isAccepted(), "落盘完成后关闭被拒绝了"
        assert os.path.exists(out_path), "落盘完成了却没有产物"
        raw = Path(out_path).read_bytes()
        assert raw.rstrip().endswith(b"%%EOF"), \
            "PDF 结尾没有 %%EOF ⇒ 是被截断的文件"
        reader = PdfReader(out_path)
        assert len(reader.pages) == 3, \
            f"PDF 页数不对（截断?）：{len(reader.pages)}"
    finally:
        win.close()


def test_close_blocked_when_save_not_finished(qapp, tmp_path, isolated_cache,
                                              monkeypatch, no_popups):
    """
    ★ A1-R2 的另一半：关窗等待**有界** ⇒ 等不到就**阻止关闭**并**明确告知用户**。

    判据：关闭被拒绝（event.accepted 为 False）+ 弹了"保存尚未完成"的警告。
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    started = threading.Event()
    real_save_pages = save_manager.save_pages

    def _very_slow_save_pages(*a, **k):
        started.set()
        time.sleep(2.0)
        return real_save_pages(*a, **k)

    monkeypatch.setattr(save_manager, "save_pages", _very_slow_save_pages)

    win = _make_window(monkeypatch, out_dir)
    try:
        # ★ 把等待上限压到 0.3s（真实默认 30s）⇒ 测试不用等 30s
        win.CLOSE_SAVE_WAIT_SECONDS = 0.3
        pages = [_write_page("job_block", i, Image.new("RGB", (60, 80), (5, 5, i)),
                              ext="png") for i in (1, 2, 3)]
        win._direct_save_result(
            _make_result("job_block", pages, ext="pdf",
                         output_path=str(out_dir / "HP_Scan_block.pdf")))
        assert started.wait(timeout=10), "落盘没起来"
        qapp.processEvents()

        t0 = time.monotonic()
        event = _close_event()
        win.closeEvent(event)
        waited = time.monotonic() - t0

        assert not event.isAccepted(), "保存没完成却把窗口关了（会留下截断文件）"
        assert waited < 1.5, f"关窗等待没有上界（等了 {waited:.1f}s）"
        assert no_popups.warning_calls, "没告诉用户有未完成的保存"
        text = " ".join(t + " " + m for t, m in no_popups.warning_calls)
        assert "保存" in text and "完成" in text, \
            f"警告文案没说明有未完成的保存: {text!r}"
    finally:
        # 等落盘自己跑完（别把后台线程留给 pytest 退出时 join）
        assert _wait_for(lambda: gui._pending_save_count(win) == 0, qapp=qapp)
        win.close()
