"""
取消扫描后「已扫出的页去哪了」的用户可见说明 — 回归测试

★ 背景（终审观察 + 独立评估）
--------------------------------------------------------------------------
取消扫描时，**页文件并没有丢** —— 它们仍在 `cache_root/<job_id>/page_NNN.ext`，
按 `cache_manager` 的 `stale_hours`（默认 24 小时）才被清理。但：

- 页元数据被 `_discard_page_meta` **丢弃**（纠偏角/裁剪框/手动旋转全没）；
- GUI 只显示一句「扫描已取消」，**不报页数、不指明去向**；
- 而**成功**时 GUI 明确说「本批 N 页已加入会话（共 M 页）」⇒ 强烈反差。

⇒ 用户会合理推断"白扫了、页没了"，多半**重扫一遍**（浪费纸和时间）。
本文件钉住：取消时必须**如实告知页数与去向**。

⚠️ 刻意**不做**的事（主控已拍板）：
- 不把已扫出的页**静默收编进会话** —— 用户取消往往正是因为不想收这批；
- 不新增模态框让用户拍板 —— 关窗/关线程时序上容易卡住，代价大于收益。
⇒ 只做"如实说明"（方案 A）。
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hp_scan_gui_pyside as gui  # noqa: E402


@pytest.fixture(scope="module")
def qapp(qt_app):
    yield qt_app


@pytest.fixture
def win(qapp, monkeypatch):
    monkeypatch.setattr(gui, "QMessageBox", _FakeBox())
    app = gui.ScanApp()
    try:
        yield app
    finally:
        try:
            app.close()
        except Exception:
            pass


class _FakeBox:
    Yes = 0x00004000
    No = 0x00010000

    def warning(self, parent, title, text):
        pass

    def critical(self, parent, title, text):
        pass

    def information(self, parent, title, text):
        pass

    def question(self, parent, title, text, *a, **k):
        return self.Yes


class TestCancelTellsWherePagesWent:

    def test_cancel_reports_page_count_and_recoverability(self, win, monkeypatch):
        """★ 取消时必须报出**已扫出的页数**并说明可找回。"""
        monkeypatch.setattr(gui.cache_manager, "list_pages",
                            lambda job_id: [(1, "jpg", "a"), (2, "jpg", "b")])
        win.status_bar.showMessage("")
        win._on_scan_cancelled("job_cancel_demo")
        msg = win.status_bar.currentMessage()
        assert "2 页" in msg, "取消提示没报页数: %r" % msg
        assert ("缓存" in msg) or ("找回" in msg), (
            "取消提示没说页还在哪: %r" % msg)

    def test_cancel_without_pages_keeps_plain_message(self, win, monkeypatch):
        """★ 一页都没扫到时 ⇒ 保持原来的「扫描已取消」（不要报 0 页）。"""
        monkeypatch.setattr(gui.cache_manager, "list_pages", lambda job_id: [])
        win.status_bar.showMessage("")
        win._on_scan_cancelled("job_cancel_empty")
        assert win.status_bar.currentMessage() == "扫描已取消"

    def test_list_pages_failure_does_not_break_cancel(self, win, monkeypatch):
        """★ 列页失败（缓存已被清理/异常）⇒ 不得让取消流程崩掉。"""
        def _boom(job_id):
            raise OSError("缓存目录已不存在")

        monkeypatch.setattr(gui.cache_manager, "list_pages", _boom)
        win.status_bar.showMessage("")
        win._on_scan_cancelled("job_cancel_broken")
        assert win.status_bar.currentMessage() == "扫描已取消"
