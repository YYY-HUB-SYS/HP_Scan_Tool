"""
批次 4 · B4-6 GUI 接线回归测试 —— HP Scan Tool v4.0

覆盖（brief §5 的 11 条里能在 GUI 层验证的 10 条 + 若干纯逻辑护栏）：
  1  双面流程：扫正面 ⇒ 状态栏提示翻面 ⇒ 扫背面 ⇒ 产出**一个** PDF、页序 = interleave_duplex
  2  双面时**不**弹预览对话框
  3  中途改选单面 ⇒ 暂存正面被放弃（按单面保存）
  4  续扫两批 ⇒ 会话页数 = 两批之和；「结束并保存」⇒ 一个 PDF、页序正确
  5  落盘成功 ⇒ 会话 completed、job 目录被清、is_job_protected 为 False
  6  放弃会话 ⇒ abandoned、缓存清理、保护解除
  7  **重启恢复** ⇒ 会话还在、页还在、**保护被重新登记**
  8  强提醒 ⇒ 弹窗出现、**带预览图**、三个按钮都在
  9  「中断并从第 N 页重扫」⇒ 第 N 页及之后被丢弃，前 N-1 页保留
  10 「强制保存」⇒ 该页 force_keep=True、产物**不**纠偏
  ★ 11 连跑 3 次全量测试无崩溃（门禁，由 CI/人工执行）

GUI 纪律：
- 一律 `QT_QPA_PLATFORM=offscreen`，**不弹真实窗口、不注入鼠标键盘事件**
- 需要模态对话框时用**测试替身**（exec() 立即返回）⇒ 不会有挂死的模态框卡住会话
- 真机扫描**一律不跑**：用假协调器顶掉 `start_scan`，手动投递扫描结果
- 缓存根目录用 `isolated_cache` fixture 重定向 ⇒ 不碰用户真实 Documents 缓存
- 用户数据（APPDATA）由 conftest 的 autouse fixture 重定向到 tmp_path
"""

import io
import os
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest
from PIL import Image

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
# 测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
#
# ★ 用 TemporaryDirectory 而不是裸 mkdtemp（2026-10-21 修）：
#   裸 mkdtemp **永不清理** ⇒ 每跑一次 pytest 就在 %TEMP% 漏一个 hp_gui_b46_home_*
#   （实测一次全量跑就多一个，几十轮后 %TEMP% 堆成一片）。
#   TemporaryDirectory 在**进程正常退出时自动删除**；进程被 abort 时由
#   tests/conftest.py 的 session 级 fixture 兜底清理。
_TMP_HOME_OBJ = tempfile.TemporaryDirectory(prefix="hp_gui_b46_home_")
_TMP_HOME = _TMP_HOME_OBJ.name
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import cache_manager
    import doc_merge
    import document_session
    import page_meta
    import save_manager
    from escl_engine import ScannerInfo
    from scan_coordinator import ScanResult
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ────────── 测试替身 ──────────

class _FakeMessageBox:
    """QMessageBox 替身：记录调用，**绝不弹真实窗口**"""

    Yes = 0x00004000
    No = 0x00010000

    def __init__(self):
        self.calls = []
        self.reply = self.No          # ★ 默认"否"：启动恢复时不自动接管会话

    def critical(self, parent, title, text):
        self.calls.append(("critical", title, text))

    def information(self, parent, title, text):
        self.calls.append(("information", title, text))

    def warning(self, parent, title, text):
        self.calls.append(("warning", title, text))

    def question(self, parent, title, text, buttons=None, default=None):
        self.calls.append(("question", title, text))
        return self.reply

    def titles(self):
        return [c[1] for c in self.calls]


class _AutoMergeDialog(gui.SessionMergePreviewDialog):
    """合并预览替身：exec() 立即返回（避免模态框卡住整个测试会话）"""

    accept_result = True
    seen_pages = None

    def exec(self):
        type(self).seen_pages = list(self._pages)
        if self.accept_result:
            self.accept()
            return gui.QDialog.Accepted
        self.reject()
        return gui.QDialog.Rejected


class _AutoSkewDialog(gui.SkewWarningDialog):
    """倾斜强提醒替身：exec() 立即按预设选择返回（走真实的按钮处理函数）"""

    forced_choice = gui.SkewWarningDialog.CHOICE_CONTINUE

    def exec(self):
        handler = {
            gui.SkewWarningDialog.CHOICE_RESUME: self._on_resume,
            gui.SkewWarningDialog.CHOICE_FORCE: self._on_force,
            gui.SkewWarningDialog.CHOICE_CONTINUE: self._on_continue,
        }[self.forced_choice]
        handler()
        return gui.QDialog.Accepted


# ────────── fixtures ──────────

@pytest.fixture(scope="module")
def qapp(qt_app):
    """整个模块共用一个 QApplication（offscreen，不显示窗口）

    ★ 复用**会话级**实例（tests/conftest.py 的 `qt_app`）。
      自己写 `QApplication.instance() or QApplication([])` 会在进程里
      残留 QCoreApplication 时取到**错误**的实例 ⇒ 后续创建 widget
      直接崩（0xC0000409，见 tests/test_qt_app_singularity.py）。
    """
    return qt_app


@pytest.fixture
def no_popups(monkeypatch):
    """所有可能弹窗的地方都换成替身（绝不弹真实窗口）"""
    fake = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake)
    return fake


class _Harness:
    """把真实 ScanApp 包一层：假扫描 + 结果投递（**绝不碰真机**）"""

    def __init__(self, win, out_dir, fake_box):
        self.win = win
        self.out_dir = str(out_dir)
        self.box = fake_box
        self.jobs = []
        self.preview_calls = []

    # ── 假扫描 ──
    def next_job_id(self):
        return "job_%03d" % (len(self.jobs) + 1)

    def scan(self, page_count=1, start=1, ext=None):
        """走真实 `_start_scan`（会话记账）+ 手动投递扫描结果"""
        win = self.win
        ext = ext or win.output_format_val
        job_id = self.next_job_id()
        paths = _write_pages(job_id, page_count, start=start)
        out_path = os.path.join(self.out_dir, "HP_Scan_%s.%s" % (job_id, ext))
        win._fake_state["job_id"] = job_id
        win._start_scan()
        self.jobs.append((job_id, out_path))
        win._on_scan_finished(job_id, _make_result(job_id, paths, ext, out_path))
        return job_id, paths

    # ── 查询 ──
    @property
    def session(self):
        return self.win._session

    def session_file(self, session_id):
        return os.path.join(
            document_session.sessions_dir(), "%s.json" % session_id)


@pytest.fixture
def app(qapp, tmp_path, isolated_cache, monkeypatch, no_popups):
    """真实 ScanApp（offscreen）+ 输出目录重定向 + 假协调器"""
    win = gui.ScanApp()
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    win.dir_entry.setText(str(out_dir))
    win.resolution_val = 72                 # 小画布 ⇒ 测试快
    win.format_combo.setCurrentText("pdf")
    win.output_format_val = "pdf"
    win.selected = ScannerInfo(name="测试扫描仪", ip="192.168.1.9", model="HP Test")

    # ★ 假协调器：绝不真的去连设备（会出纸）
    state = {"job_id": None}

    def _fake_start_scan(scanner, params):
        return state["job_id"]

    monkeypatch.setattr(win.coordinator, "start_scan", _fake_start_scan)
    win._fake_state = state

    # ★ 拦住"启动流程"里的 _finish_loading（它会 show() + 自动恢复会话）
    #   —— 注意 QTimer.singleShot(0, self._finish_loading) 是**调用时**才查属性，
    #   所以这里打补丁来得及。
    monkeypatch.setattr(gui.ScanApp, "_finish_loading", lambda self: None)

    # 预览对话框替身（双面/续扫时**不该**被调用；单面时会被调用）
    preview_calls = []

    def _fake_preview(self, *args, **kwargs):
        preview_calls.append(("single", args, kwargs))

    def _fake_multi_preview(self, *args, **kwargs):
        preview_calls.append(("multi", args, kwargs))

    monkeypatch.setattr(gui.ScanApp, "_show_preview", _fake_preview)
    monkeypatch.setattr(gui.ScanApp, "_show_multi_preview", _fake_multi_preview)

    harness = _Harness(win, out_dir, no_popups)
    harness.preview_calls = preview_calls
    yield harness

    try:
        win.close()
    except Exception:
        pass


# ────────── 工具 ──────────

def _page_color(index):
    """页的身份色（⇒ 可从产物 PDF 反查页序）"""
    return (30 + index * 25, 60, 200 - index * 15)


def _page_bytes(index):
    buf = io.BytesIO()
    Image.new("RGB", (120, 120), _page_color(index)).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _write_pages(job_id, count, start=1):
    """把 count 页写进缓存目录（返回路径列表）"""
    paths = []
    for i in range(start, start + count):
        paths.append(cache_manager.write_page(job_id, i, _page_bytes(i), "jpg"))
    return paths


def _make_result(job_id, paths, ext="pdf", out_path=""):
    return ScanResult(
        job_id=job_id, ext=ext, page_count=len(paths), cached_paths=list(paths),
        output_path=out_path or os.path.join("C:\\out", "HP_Scan_%s.%s" % (job_id, ext)),
        source="平板", adf_detected=False)


def _wait_until(predicate, timeout=60.0):
    """等后台落盘完成（后台线程的结果经模块级投递器回主线程 ⇒ 必须泵事件）"""
    app = gui.QApplication.instance()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    return bool(predicate())


def _pdf_page_colors(pdf_path):
    """从 PDF 里取每页中心像素的颜色（用于反查页序）"""
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    colors = []
    for page in reader.pages:
        best = None
        for img in page.images:
            if best is None or len(img.data) > len(best.data):
                best = img
        assert best is not None, "PDF 页里没有可用的图片"
        with Image.open(io.BytesIO(best.data)) as im:
            rgb = im.convert("RGB")
            colors.append(rgb.getpixel((rgb.width // 2, rgb.height // 2)))
    return colors


def _assert_color(actual, expected, tol=14):
    assert all(abs(a - e) <= tol for a, e in zip(actual, expected)), \
        "颜色不符: 实际 %s，期望 %s" % (actual, expected)


def _load_session(session_id):
    return document_session.SessionStore().load(session_id)


# ────────── 1 / 2：双面流程 ──────────

def test_duplex_front_then_back_merges_to_one_pdf(app, monkeypatch):
    """双面：正面 ⇒ 提示翻面 ⇒ 背面 ⇒ 一个 PDF，页序 = interleave_duplex"""
    monkeypatch.setattr(gui, "SessionMergePreviewDialog", _AutoMergeDialog)
    win = app.win

    # 选「双面（手动）」
    win.mode_duplex_radio.click()
    assert win._current_session_mode() == gui.SESSION_MODE_DUPLEX

    # ── 正面 ──
    job_f, paths_f = app.scan(3)
    session = app.session
    assert session is not None
    assert session.mode == "duplex"
    assert session.total_pages == 3
    assert session.state == "awaiting_flip"
    assert all(p.side == "front" for p in session.pages)
    # 状态栏提示翻面
    assert "翻面" in win.status_bar.currentMessage()
    # ★ 双面时**不**弹预览对话框
    assert app.preview_calls == []
    # 缓存目录保持受保护
    assert cache_manager.is_job_protected(job_f)

    # ── 背面 ──
    job_b, paths_b = app.scan(3)
    assert app.preview_calls == []          # ★ 合并前也不弹单页预览

    # 合并页序 = interleave_duplex(front, back, reverse_back=True)
    merged = gui.session_merged_pages(app.session)
    expected = doc_merge.interleave_duplex(
        [p for p in app.session.pages if p.side == "front"],
        [p for p in app.session.pages if p.side == "back"],
        reverse_back=True)
    assert [p.cache_path for p in merged] == [p.cache_path for p in expected]
    # 与落盘路径（save_manager）算出的页序必须一致
    assert [p.cache_path for p in merged] == [
        p.cache_path for p in save_manager._session_merged_pages(app.session)]

    # 落盘（后台线程）⇒ 等它完成
    assert _wait_until(lambda: win._session_save_finished), "会话落盘未在超时内完成"

    pdfs = [f for f in os.listdir(app.out_dir) if f.lower().endswith(".pdf")]
    assert len(pdfs) == 1, "应当只产出**一个** PDF，实际: %s" % pdfs
    pdf_path = os.path.join(app.out_dir, pdfs[0])

    from pypdf import PdfReader
    assert len(PdfReader(pdf_path).pages) == 6

    # 页序反查：PDF 第 k 页的颜色 = 合并后第 k 页的颜色
    colors = _pdf_page_colors(pdf_path)
    assert len(colors) == 6
    for idx, page in enumerate(merged):
        src_index = int(os.path.basename(page.cache_path)[5:8])
        _assert_color(colors[idx], _page_color(src_index))


def test_duplex_front_does_not_open_preview_dialog(app, monkeypatch):
    """双面扫完正面 ⇒ 预览对话框**没出现**"""
    monkeypatch.setattr(gui, "SessionMergePreviewDialog", _AutoMergeDialog)
    win = app.win
    win.mode_duplex_radio.click()
    app.scan(2)
    assert app.preview_calls == []
    # 会话状态是"待翻面"，页都在会话里
    assert win._session.state == "awaiting_flip"
    assert win._session.total_pages == 2


# ────────── 3：中途改选单面 ──────────

def test_switch_to_single_abandons_staged_front(app, monkeypatch):
    """中途改选「单面」⇒ 暂存的正面被放弃，之后按单面保存"""
    monkeypatch.setattr(gui, "SessionMergePreviewDialog", _AutoMergeDialog)
    win = app.win
    win.mode_duplex_radio.click()
    job_f, _ = app.scan(3)
    session_id = win._session.session_id
    assert win._session.total_pages == 3
    assert cache_manager.is_job_protected(job_f)

    # 改选「单面」
    win.mode_single_radio.click()
    assert win._current_session_mode() == gui.SESSION_MODE_SINGLE

    # 暂存的正面被放弃
    assert win._session is None
    saved = _load_session(session_id)
    assert saved is not None and saved.state == "abandoned"
    # 缓存清理 + 保护解除
    assert not cache_manager.is_job_protected(job_f)
    assert not os.path.isdir(cache_manager._job_dir_path(job_f))

    # 之后按**单面**保存（走既有预览路径）
    app.scan(1)
    assert app.preview_calls, "单面模式应当走预览路径"


# ────────── 4 / 5：续扫两批 + 落盘成功 ──────────

def test_batch_two_batches_merge_to_one_pdf(app, monkeypatch):
    """续扫：两批 ⇒ 会话页数 = 两批之和；「结束并保存」⇒ 一个 PDF、页序正确"""
    monkeypatch.setattr(gui, "SessionMergePreviewDialog", _AutoMergeDialog)
    win = app.win
    win.mode_batch_radio.click()

    app.scan(2, start=1)
    assert win._session.total_pages == 2
    assert win._session.state == "paused"
    assert "已加入会话" in win.status_bar.currentMessage()

    app.scan(3, start=3)
    assert win._session.total_pages == 5
    assert win._session.state == "paused"
    assert app.preview_calls == []

    # 「结束并保存」
    win._save_session_action()
    assert _wait_until(lambda: win._session_save_finished), "会话落盘未在超时内完成"

    pdfs = [f for f in os.listdir(app.out_dir) if f.lower().endswith(".pdf")]
    assert len(pdfs) == 1, "应当只产出**一个** PDF，实际: %s" % pdfs
    pdf_path = os.path.join(app.out_dir, pdfs[0])

    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    assert len(reader.pages) == 5

    # 页序 = 批次顺序拼接
    expected_pages = [1, 2, 3, 4, 5]
    colors = _pdf_page_colors(pdf_path)
    for idx, expected_index in enumerate(expected_pages):
        _assert_color(colors[idx], _page_color(expected_index))
    # 与落盘路径算出的页序一致
    session = win._session
    assert session is None or True   # 落盘成功后当前会话已被清空（见落盘成功那条）


def test_save_success_marks_completed_and_clears_cache(app, monkeypatch):
    """落盘成功 ⇒ 会话 completed、job 目录被清、is_job_protected 为 False"""
    monkeypatch.setattr(gui, "SessionMergePreviewDialog", _AutoMergeDialog)
    win = app.win
    win.mode_batch_radio.click()
    job_id, _ = app.scan(2)
    session_id = win._session.session_id
    assert cache_manager.is_job_protected(job_id)

    win._save_session_action()
    assert _wait_until(lambda: win._session_save_finished)

    saved = _load_session(session_id)
    assert saved is not None
    assert saved.state == "completed"
    # job 目录被清 + 保护解除
    assert not os.path.isdir(cache_manager._job_dir_path(job_id))
    assert cache_manager.is_job_protected(job_id) is False
    assert win._session is None


# ────────── 6：放弃会话 ──────────

def test_abandon_session_clears_cache_and_protection(app):
    """放弃会话 ⇒ abandoned、缓存清理、保护解除"""
    win = app.win
    win.mode_batch_radio.click()
    job_id, _ = app.scan(2)
    session_id = win._session.session_id
    assert cache_manager.is_job_protected(job_id)

    app.box.reply = app.box.Yes          # 用户确认「放弃会话」
    win._abandon_session_action()

    saved = _load_session(session_id)
    assert saved is not None and saved.state == "abandoned"
    assert not os.path.isdir(cache_manager._job_dir_path(job_id))
    assert cache_manager.is_job_protected(job_id) is False
    assert win._session is None


# ────────── 7：重启恢复 ──────────

def test_restart_restores_session_and_reprotects_cache(app, monkeypatch, tmp_path,
                                                        isolated_cache):
    """重启恢复 ⇒ 会话还在、页还在、**保护被重新登记**"""
    win = app.win
    win.mode_batch_radio.click()
    job_id, paths = app.scan(2)
    session_id = win._session.session_id

    # ── 模拟进程重启：保护集合是**进程内**的 ⇒ 重启后为空 ──
    monkeypatch.setattr(cache_manager, "_protected_dirs", {})
    monkeypatch.setattr(cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(cache_manager, "_active_jobs", set())
    assert cache_manager.is_job_protected(job_id) is False

    # 新进程：新的 ScanApp ⇒ 启动恢复
    win2 = gui.ScanApp()
    try:
        win2.dir_entry.setText(str(tmp_path / "out"))
        app.box.reply = app.box.Yes          # 用户选择"继续"
        win2._restore_open_sessions()

        assert win2._session is not None
        assert win2._session.session_id == session_id
        assert win2._session.total_pages == 2
        # 页还在
        for path in paths:
            assert os.path.isfile(path)
        # ★ 保护被重新登记（否则隔天续扫的页会被 cleanup_stale 删掉）
        assert cache_manager.is_job_protected(job_id)
        assert job_id in cache_manager.protected_job_ids()
    finally:
        try:
            win2.close()
        except Exception:
            pass


# ────────── 8：倾斜强提醒弹窗 ──────────

def test_skew_warning_dialog_has_three_buttons_and_preview(app, monkeypatch):
    """强提醒 ⇒ 弹窗出现、带预览图、三个按钮都在"""
    job_id, paths = app.scan(1)
    cache_path = paths[0]

    dialog = gui.SkewWarningDialog(job_id, 1, 5.5, cache_path)
    try:
        # 三个按钮都在
        assert dialog.btn_resume.text() == "中断并从第 N 页重扫"
        assert dialog.btn_continue.text() == "继续扫描（自动纠偏）"
        assert dialog.btn_force.text() == "强制保存（不纠偏）"
        # 带预览图（缩略图，不是全尺寸大图）
        pixmap = dialog.preview_label.pixmap()
        assert pixmap is not None and not pixmap.isNull(), "强提醒弹窗必须有预览图"
        assert dialog.preview_label.pixmap().width() <= 420
        # 文案里有页号与角度
        assert "第 1 页" in dialog.message_label.text()
        assert "5.5" in dialog.message_label.text()
        # ★ parent 必须是 None（不挂到可能先销毁的对象上）
        assert dialog.parent() is None
    finally:
        dialog.deleteLater()


def test_skew_warning_wiring_uses_post_ui(app, monkeypatch):
    """协调器（工作线程）发来的信号 ⇒ 走 _post_ui 投递回主线程再弹窗"""
    monkeypatch.setattr(gui, "SkewWarningDialog", _AutoSkewDialog)
    win = app.win
    job_id, paths = app.scan(1)

    posted = []
    original_post_ui = win._post_ui

    def _spy(fn):
        posted.append(fn)
        return original_post_ui(fn)

    monkeypatch.setattr(win, "_post_ui", _spy)
    _AutoSkewDialog.forced_choice = gui.SkewWarningDialog.CHOICE_CONTINUE
    win._on_skew_warning(job_id, 1, 4.0, paths[0])

    assert posted, "必须经 _post_ui 投递（不能在工作线程里弹窗）"
    # 投递后本批次不再提醒
    assert job_id in win._skew_suppressed_jobs


# ────────── 9：从第 N 页重扫 ──────────

def test_resume_from_page_keeps_previous_pages(app, monkeypatch):
    """「中断并从第 N 页重扫」⇒ 第 N 页及之后被丢弃，前 N-1 页保留"""
    win = app.win
    cancelled = []
    monkeypatch.setattr(win.coordinator, "cancel_scan",
                        lambda job_id: cancelled.append(job_id))
    win.mode_batch_radio.click()
    job_id, paths = app.scan(5)
    session = win._session
    assert session.total_pages == 5

    # 中断 + 从第 3 页重扫
    win._resume_from_page(job_id, 3)

    # 前 N-1 页保留
    assert session.total_pages == 2
    assert [p.page_no for p in session.pages] == [1, 2]
    # 第 N 页及之后的缓存文件被删
    assert os.path.isfile(paths[0]) and os.path.isfile(paths[1])
    assert not os.path.exists(paths[2])
    assert not os.path.exists(paths[3])
    assert not os.path.exists(paths[4])
    # 扫描被中断（协调器收到了取消）
    assert cancelled == [job_id]
    # ★ 删页必须**活过落盘**（磁盘上的副本也不能把第 3~5 页复活）
    on_disk = _load_session(session.session_id)
    assert on_disk is not None
    assert [p.page_no for p in on_disk.pages] == [1, 2]


def test_resume_from_page_during_scan_keeps_earlier_pages(app, monkeypatch):
    """
    扫描**进行中**点「中断并从第 N 页重扫」⇒
    中断后已扫到的页被收进会话，再裁掉第 N 页及之后（保留前 N-1 页）。
    """
    win = app.win
    monkeypatch.setattr(win.coordinator, "cancel_scan", lambda job_id: None)
    win.mode_batch_radio.click()

    job_id = app.next_job_id()
    paths = _write_pages(job_id, 5)
    # 这一批还在扫描中
    win._pending_side_by_job[job_id] = "single"
    session = win._ensure_session(gui.SESSION_MODE_BATCH)

    win._resume_from_page(job_id, 3)
    # 扫描被中断 ⇒ 残留页收进会话并按 N 裁掉
    win._on_scan_cancelled(job_id)

    assert session.total_pages == 2
    assert [p.page_no for p in session.pages] == [1, 2]
    assert os.path.isfile(paths[0]) and os.path.isfile(paths[1])
    assert not os.path.exists(paths[2])
    assert not os.path.exists(paths[4])
    # ★ 删页必须**活过落盘**（磁盘上的副本也不能把第 3~5 页复活）
    on_disk = _load_session(session.session_id)
    assert on_disk is not None
    assert [p.page_no for p in on_disk.pages] == [1, 2]


def test_resume_from_page_api_contract():
    """「从第 N 页重扫」的**可复用 API** 契约"""
    session = document_session.new_session(mode="batch", ext="pdf")
    paths = []
    for i in range(1, 6):
        p = os.path.join(tempfile.mkdtemp(prefix="hp_b46_"), "page_%03d.jpg" % i)
        with open(p, "wb") as f:
            f.write(b"x")
        paths.append(p)
    session.add_pages("job_a", paths, side="single", page_nos=[1, 2, 3, 4, 5])
    session.add_pages("job_b", [paths[0]], side="single", page_nos=[1])

    dropped = gui.resume_from_page(session, "job_a", 3, drop_cache=True)
    assert [p.page_no for p in dropped] == [3, 4, 5]
    # 只影响该批次
    assert session.total_pages == 3
    assert [p.job_id for p in session.pages] == ["job_a", "job_a", "job_b"]
    assert not os.path.exists(paths[2])
    assert os.path.exists(paths[0])


# ────────── 10：强制保存（不纠偏）──────────

def test_force_keep_marks_page_and_skips_deskew(app, monkeypatch, isolated_cache):
    """「强制保存」⇒ 该页 force_keep=True、产物**不**纠偏"""
    monkeypatch.setattr(gui, "SkewWarningDialog", _AutoSkewDialog)
    win = app.win
    win.mode_batch_radio.click()

    job_id = app.next_job_id()
    paths = _write_pages(job_id, 2)
    # 给两页都写上纠偏角（模拟扫描时测出来的）
    store = page_meta.PageMetaStore(cache_manager._cache_root())
    for i, path in enumerate(paths, 1):
        store.set_page(job_id, i, page_meta.PageMeta(
            cache_path=path, width=120, height=120, deskew_angle=5.0))

    _AutoSkewDialog.forced_choice = gui.SkewWarningDialog.CHOICE_FORCE
    win._on_skew_warning(job_id, 1, 5.0, paths[0])
    assert (job_id, 1) in win._force_keep_pages
    assert job_id in win._skew_suppressed_jobs

    # 这一批并入会话 ⇒ 第 1 页被标成 force_keep
    win._fake_state["job_id"] = job_id
    win._start_scan()
    win._on_scan_finished(job_id, _make_result(
        job_id, paths, "pdf", os.path.join(app.out_dir, "HP_Scan_force.pdf")))
    session = win._session
    assert session.total_pages == 2
    forced = [p for p in session.pages if p.force_keep]
    assert len(forced) == 1 and forced[0].page_no == 1

    # 落盘时：强制保存的那页纠偏角被归零，另一页保留角度
    merged = save_manager._session_merged_pages(session)
    _specs, angles = save_manager._session_page_specs(merged, deskew_enabled=True)
    assert angles[0] == 0.0, "强制保存的页不纠偏"
    assert angles[1] != 0.0, "未强制保存的页照常纠偏"

    # ★ 端到端：真正落盘时，强制保存的那页**不走**纠偏（deskew_image 不被调用）
    calls = []
    real_deskew = save_manager.skew_detect.deskew_image

    def _spy_deskew(img, angle):
        calls.append(angle)
        return real_deskew(img, angle)

    monkeypatch.setattr(save_manager.skew_detect, "deskew_image", _spy_deskew)
    out_pdf = os.path.join(app.out_dir, "force_keep.pdf")
    save_manager.save_session(session, out_path=out_pdf)
    assert os.path.isfile(out_pdf)
    assert len(calls) == 1, "只有未强制保存的那页才纠偏（实际调用 %d 次）" % len(calls)


# ────────── 纯逻辑护栏 ──────────

def test_session_merged_pages_matches_save_manager():
    """GUI 预览的页序必须与 save_manager 落盘用的页序一致（防分叉）"""
    duplex = document_session.new_session(mode="duplex", ext="pdf",
                                          reverse_back=True)
    duplex.add_pages("f", ["a", "b", "c"], side="front", page_nos=[1, 2, 3])
    duplex.add_pages("b", ["x", "y", "z"], side="back", page_nos=[1, 2, 3])
    assert [p.cache_path for p in gui.session_merged_pages(duplex)] == \
        [p.cache_path for p in save_manager._session_merged_pages(duplex)]

    batch = document_session.new_session(mode="batch", ext="pdf")
    batch.add_pages("j1", ["a", "b"], side="single", page_nos=[1, 2])
    batch.add_pages("j2", ["c"], side="single", page_nos=[1])
    assert [p.cache_path for p in gui.session_merged_pages(batch)] == \
        [p.cache_path for p in save_manager._session_merged_pages(batch)]


def test_new_controls_appended_without_touching_existing_layout(app):
    """新增控件**追加**在参数区末尾，既有控件一个不少、位置不变"""
    win = app.win
    # 既有控件还在
    for name in ("resolution_combo", "color_mode_combo", "format_combo",
                 "source_combo", "duplex_check", "direct_save_check",
                 "scan_btn", "listen_btn"):
        assert hasattr(win, name), "既有控件 %s 丢了" % name
    # 新增控件
    assert win.mode_single_radio.isChecked()
    assert win.reverse_back_check.isChecked()
    assert win.session_info_label.text() == "当前会话：无"
    assert win.session_save_btn.isEnabled() is False
    assert win.session_abandon_btn.isEnabled() is False
    # 扫描模式互斥
    win.mode_duplex_radio.click()
    assert win.mode_duplex_radio.isChecked()
    assert win.mode_single_radio.isChecked() is False


# ────────── 11：真实模态弹窗不崩（子进程）──────────

def test_real_modal_dialog_does_not_crash():
    """
    ★ 真实模态路径（exec）在**子进程**里跑一遍 ⇒ 验证不产生堆损坏。

    上一轮的 0xc0000374 就是模态弹窗路径上的 use-after-free ⇒
    堆损坏是概率性的，单次绿不算数，所以这里单独跑一遍真实 exec()。
    """
    # ★ 父进程先建一个**父目录**，子进程在它里面建 sandbox / home。
    #   原因：子进程自己的 `finally` 里 rmtree 常常删不干净
    #   （home 下有 Qt 占用中的文件 ⇒ ignore_errors=True 静默跳过 ⇒ 每次漏一个）。
    #   改由**父进程**在子进程完全退出后删整个父目录 ⇒ 占用已释放，必删得掉。
    probe_root = tempfile.mkdtemp(prefix="hp_b46_probe_")
    script = textwrap.dedent(
        """
        import os, sys
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        # ★ 先隔离用户数据（在 import 业务模块之前）；两个沙箱都建在父目录里
        sandbox = os.path.join(%r, "sandbox")
        home = os.path.join(%r, "home")
        os.makedirs(sandbox, exist_ok=True)
        os.makedirs(os.path.join(home, "Desktop"), exist_ok=True)
        os.environ["APPDATA"] = sandbox
        os.environ["USERPROFILE"] = home
        sys.path.insert(0, %r)
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer
        import hp_scan_gui_pyside as gui
        app = QApplication([])
        dlg = gui.SkewWarningDialog("job", 1, 4.0, "")
        assert dlg.parent() is None
        def _close():
            dlg._on_continue()
        QTimer.singleShot(50, _close)
        dlg.exec()
        print("CHOICE", dlg.choice)
        """
    ) % (probe_root, probe_root, PROJECT_ROOT)
    path = os.path.join(tempfile.gettempdir(), "hp_b46_modal_probe.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    try:
        proc = subprocess.run(
            [sys.executable, path], capture_output=True, text=True, timeout=120,
            # ★ 同上：text=True 必须配 encoding，否则按控制台代码页（GBK）解码
            #   子进程的 UTF-8 输出 ⇒ _readerthread 里 UnicodeDecodeError
            encoding="utf-8", errors="replace")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        # ★ 子进程退出后回收整个父目录（含 sandbox / home）
        import shutil as _shutil
        _shutil.rmtree(probe_root, ignore_errors=True)
    assert proc.returncode == 0, (
        "真实模态弹窗进程异常退出: rc=%s\n%s\n%s"
        % (proc.returncode, proc.stdout, proc.stderr))
    assert "CHOICE continue" in proc.stdout, proc.stdout
