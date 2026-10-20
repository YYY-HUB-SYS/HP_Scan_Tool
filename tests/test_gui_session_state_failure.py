"""
会话落盘的「状态写失败」在 GUI 侧必须被如实处理 — 回归测试

★ 背景（终审 Important：幽灵会话）
--------------------------------------------------------------------------
`save_manager.save_session` 现在会：① 写会话状态并**回读验证** → ② 才清缓存。
状态写失败时它会**保留缓存**、把失败如实放进 `SaveResult`：

    status_write_failed : bool
    status_write_error  : str
    session_state       : str

⇒ GUI 侧若不知道这件事，就会做出**两个致命动作**：
  ① `cleanup_session_jobs(session)` ⇒ 把 save_manager 特意保留的缓存**清掉**
     ⇒ 会话仍开着、却指向**死路径** ⇒ 这才是真正的幽灵会话；
  ② `self._session = None` ⇒ 用户**无法重试**，会话凭空消失。

★ 本文件钉住：状态写失败 ⇒ 不清缓存、不丢会话、且**让用户看见**。
"""

import os
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import document_session  # noqa: E402
import hp_scan_gui_pyside as gui  # noqa: E402


@pytest.fixture(scope="module")
def qapp(qt_app):
    yield qt_app


class _FakeBox:
    Yes = 0x00004000
    No = 0x00010000

    def __init__(self):
        self.warning_calls = []
        self.information_calls = []
        self.critical_calls = []

    def warning(self, parent, title, text):
        self.warning_calls.append((title, text))

    def information(self, parent, title, text):
        self.information_calls.append((title, text))

    def critical(self, parent, title, text):
        self.critical_calls.append((title, text))

    def question(self, parent, title, text, *a, **k):
        return self.Yes


class _FakeResult(list):
    """SaveResult 替身：它本来就是 list 子类（额外带 .failed_pages 等字段）"""

    def __init__(self, **kw):
        super().__init__(["out_001.pdf"])
        self.failed_pages = 0
        self.skipped_pages = []
        self.warnings = []
        self.status_write_failed = False
        self.status_write_error = ""
        self.session_state = ""
        for k, v in kw.items():
            setattr(self, k, v)


def _result(**kw):
    return _FakeResult(**kw)


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "QMessageBox", _FakeBox())
    app = gui.ScanApp()
    try:
        yield app
    finally:
        try:
            app.close()
        except Exception:
            pass


class TestSessionStateWriteFailure:

    def test_status_write_failure_keeps_cache_and_session(self, win, monkeypatch):
        """★ 状态写失败 ⇒ 不得清缓存、不得丢弃会话。"""
        cleaned = []
        monkeypatch.setattr(gui, "cleanup_session_jobs",
                            lambda s: cleaned.append(s))

        session = document_session.new_session(mode="batch", session_id="s_ghost")
        win._session = session

        win._on_session_saved(_result(status_write_failed=True,
                                      status_write_error="锁超时"))

        assert cleaned == [], (
            "状态写失败时清掉了缓存 ⇒ 会话仍开着却指向死路径（真幽灵会话）")
        assert win._session is not None, (
            "状态写失败时丢弃了会话 ⇒ 用户无法重试")

    def test_status_write_failure_is_visible_to_user(self, win, monkeypatch):
        """★ 状态写失败必须让用户看见（状态栏 + 弹窗），不能只写日志。"""
        fake = _FakeBox()
        monkeypatch.setattr(gui, "QMessageBox", fake)
        monkeypatch.setattr(gui, "cleanup_session_jobs", lambda s: None)

        session = document_session.new_session(mode="batch", session_id="s_vis")
        win._session = session
        win.status_bar.showMessage("")

        win._on_session_saved(_result(status_write_failed=True,
                                      status_write_error="page_meta 锁超时"))

        msg = win.status_bar.currentMessage()
        assert msg, "状态写失败后状态栏没有任何提示"
        assert ("状态" in msg) or ("会话" in msg), (
            "提示没说明是会话状态没写上: %r" % msg)
        assert fake.warning_calls or fake.critical_calls, (
            "状态写失败没有弹窗告知用户（只写日志不够）")

    def test_normal_path_still_cleans_up_and_closes_session(self, win, monkeypatch):
        """★ 正常路径维持原样：清缓存 + 关会话（不能被上面的分支误伤）。"""
        cleaned = []
        monkeypatch.setattr(gui, "cleanup_session_jobs",
                            lambda s: cleaned.append(s))

        session = document_session.new_session(mode="batch", session_id="s_ok")
        win._session = session

        win._on_session_saved(_result())

        assert cleaned == [session], "正常路径应清缓存"
        assert win._session is None, "正常路径应关闭会话"


class TestSessionOutputPathRecorded:
    """★ 完成的会话应当**记录产物路径**（否则用户找不到刚生成的文件）。"""

    def test_output_path_is_written_back_to_session(self, win, monkeypatch, tmp_path):
        session = document_session.new_session(mode="batch", session_id="s_path")
        win._session = session

        # 只捕获后台任务，不执行（避免真的去落盘）
        monkeypatch.setattr(gui, "_run_in_background",
                            lambda work, ok, err, label="": None)
        monkeypatch.setattr(gui.save_manager, "save_session",
                            lambda s, out_path=None, **kw: out_path)

        win._save_session_now(session)
        assert session.output_path, (
            "会话没有记录产物路径（磁盘上的会话因此指向不到文件）")
        assert os.path.isabs(session.output_path), (
            "记录的产物路径必须是绝对路径: %r" % session.output_path)
        assert os.path.splitext(session.output_path)[1].lower() in (
            ".jpg", ".jpeg", ".png", ".tiff", ".pdf"), (
            "记录的产物路径扩展名不对: %r" % session.output_path)
