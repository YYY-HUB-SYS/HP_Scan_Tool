"""
GUI 侧「页元数据写入失败」的用户可见提示 — 接线回归测试

★ 背景（终审 Important I-3，`final-review-protocol-orchestration.md`）
--------------------------------------------------------------------------
`page_meta.set_page()` 在「拿不到跨进程锁 / `page_meta.json` 已损坏」时返回
**False 且不抛异常** ⇒ 调用方若丢弃返回值，页元数据就**整批静默丢失**：
用户看到「扫描成功」，但那一页**既没有自动纠偏角、也没有手动旋转/裁边框**
（`save_manager` 读不到元数据就按 0 度处理）。

协调器侧已修好（记 error 日志 + 累计 `page_meta_failed_pages` +
发 `page_meta_save_failed(job_id, page_no)` 信号），但**GUI 侧还没接线**
⇒ 用户端目前**只有日志、没有提示**。

⇒ 本文件钉住 GUI 必须把这件事**让用户看见**。
"""

import ast
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hp_scan_gui_pyside as gui  # noqa: E402


@pytest.fixture(scope="module")
def qapp(qt_app):
    yield qt_app


def _class_method_source(filename: str, class_name: str, func_name: str) -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), filename)
    tree = ast.parse(open(path, "r", encoding="utf-8").read())
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == class_name:
            for node in cls.body:
                if isinstance(node, ast.FunctionDef) and node.name == func_name:
                    return ast.unparse(node)
    raise AssertionError("在 %s 的 %s 里找不到 %s"
                          % (filename, class_name, func_name))


class TestPageMetaFailureIsVisible:

    def test_scan_app_connects_page_meta_save_failed(self):
        """★ ScanApp 必须接上 `page_meta_save_failed` 信号。

        漏接线 ⇒ 协调器发了信号没人收 ⇒ 用户只有日志、没有提示。
        """
        src = _class_method_source("hp_scan_gui_pyside.py", "ScanApp", "__init__")
        assert "page_meta_save_failed" in src, (
            "ScanApp.__init__ 没有连接 page_meta_save_failed 信号 ⇒ "
            "页元数据写入失败时用户看不到任何提示")

    def test_scan_finish_reports_failed_page_meta_count(self):
        """★ 扫描结束时若 `page_meta_failed_pages > 0` ⇒ 必须汇总提示。

        只在信号触发那一刻提示不够：用户可能没盯着状态栏，
        扫描结束时（用户正要去看产物）才是该说的时机。
        """
        src = _class_method_source("hp_scan_gui_pyside.py", "ScanApp",
                                   "_on_scan_finished")
        assert "page_meta_failed_pages" in src, (
            "_on_scan_finished 没有检查 page_meta_failed_pages ⇒ "
            "扫描结束时不会汇总告知用户有几页的元数据没写上")

    def test_signal_produces_user_visible_hint(self, qapp, monkeypatch):
        """★ 发出信号 ⇒ GUI 必须给**用户可见**提示（状态栏）。"""
        import types

        class _FakeBox:
            Yes = 0x00004000
            No = 0x00010000

            def __init__(self):
                self.warning_calls = []

            def warning(self, parent, title, text):
                self.warning_calls.append((title, text))

            def critical(self, parent, title, text):
                pass

            def information(self, parent, title, text):
                pass

            def question(self, parent, title, text, *a, **k):
                return self.Yes

        fake = _FakeBox()
        monkeypatch.setattr(gui, "QMessageBox", fake)
        win = gui.ScanApp()
        try:
            win.status_bar.showMessage("")
            win.coordinator.page_meta_save_failed.emit("job_meta_fail", 3)
            msg = win.status_bar.currentMessage()
            assert msg, "页元数据写入失败后状态栏没有任何提示"
            assert ("元数据" in msg) or ("纠偏" in msg) or ("旋转" in msg), (
                "提示没说明后果（该页将没有自动纠偏/旋转）: %r" % msg)
        finally:
            try:
                win.close()
            except Exception:
                pass
