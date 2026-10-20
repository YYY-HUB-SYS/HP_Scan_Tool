"""
PDF 体积门禁（1.5GB）的确认回调 —— GUI 接线回归测试

★ 缺陷背景（终审 Important，`final-review-save-session-gui-cli.md`）
--------------------------------------------------------------------------
`save_manager` 的 `_precheck_pdf_volume` 在预计 PDF 合成内存峰值超过
**1.5GB 门禁**时，会调 `confirm(msg)` 问用户「仍要继续吗」；`confirm` 为 None 时
**只记 warning 并继续** ⇒ 门禁形同虚设。

`save_pages` / `save_session` 都支持 `confirm` 参数，**预览对话框那条路径也传了**
（`confirm=self._make_confirm_callback()`）—— 但**直存**与**会话落盘**这两条
**新主路径**都没传 ⇒ 用户在超大文档上点了保存，界面**看不到任何提示**，
直接吃到一次可能的 OOM。

⇒ 本文件钉住：
  ① `ScanApp` 必须提供 `_make_confirm_callback()`（回主线程弹窗 + 阻塞等待）；
  ② **直存**与**会话落盘**两条路径都必须把它接上（AST 接线断言 ——
     漏接线是这类缺陷的典型形态，行为测试很难自然覆盖）；
  ③ 单页保存路径**不得**因多页新增的 confirm 参数而 TypeError。
"""

import ast
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hp_scan_gui_pyside as gui  # noqa: E402


@pytest.fixture(scope="module")
def qapp(qt_app):
    yield qt_app


# ── ① ScanApp 必须提供 confirm 回调工厂 ────────────────────────────────

class TestConfirmCallbackFactory:

    def test_scan_app_exposes_confirm_callback_factory(self):
        """★ ScanApp 必须有 `_make_confirm_callback`（否则两条主路径没法接门禁）。"""
        assert hasattr(gui.ScanApp, "_make_confirm_callback"), (
            "ScanApp 缺少 _make_confirm_callback ⇒ "
            "直存/会话落盘无法接上 PDF 体积门禁的确认回调")

    def test_callback_runs_the_dialog_on_the_main_thread(self, qapp, monkeypatch):
        """★ 回调可能在**后台线程**被调用（落盘线程）⇒ 弹窗必须回主线程。"""
        import types

        seen = {}

        class _FakeBox:
            Yes = 0x00004000
            No = 0x00010000

            @staticmethod
            def question(parent, title, text, *args, **kwargs):
                seen["thread"] = threading.current_thread()
                return _FakeBox.Yes

        monkeypatch.setattr(gui, "QMessageBox", _FakeBox)
        stub = types.SimpleNamespace()
        cb = gui.ScanApp._make_confirm_callback(stub)
        assert callable(cb)

        result = {}

        def _worker():
            result["value"] = cb("峰值 2GB，仍要继续吗？")

        t = threading.Thread(target=_worker)
        t.start()
        # ★ 必须**泵主线程事件** —— 回调把弹窗投递回主线程，
        #   没人泵事件的话 `done.wait()` 会永远阻塞（这个坑踩过）。
        deadline = time.time() + 15
        while "thread" not in seen and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        t.join(timeout=15)
        assert not t.is_alive(), "回调没有返回（可能没回主线程弹窗）"
        assert result.get("value") is True, "用户答「继续」时应返回 True"
        assert seen.get("thread") is threading.main_thread(), (
            "确认弹窗必须在**主线程**执行，实际在 %s" % seen.get("thread"))


# ── ② 直存 / 会话落盘 两条路径都必须接上 confirm（AST 接线断言）──────────

def _function_source(filename: str, func_name: str,
                     class_name: str | None = None) -> str:
    """取函数的源码文本；`class_name` 给了就在**那个类里**找（避免同名歧义）。"""
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), filename)
    tree = ast.parse(open(path, "r", encoding="utf-8").read())
    if class_name:
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef) and cls.name == class_name:
                for node in cls.body:
                    if isinstance(node, ast.FunctionDef) and node.name == func_name:
                        return ast.unparse(node)
        raise AssertionError("在 %s 的 %s 里找不到函数 %s"
                              % (filename, class_name, func_name))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.unparse(node)
    raise AssertionError("在 %s 里找不到函数 %s" % (filename, func_name))


class TestVolumeGateWiring:
    """★ 漏接线是这类缺陷的典型形态 ⇒ 用 AST 断言函数体里确实传了 confirm。"""

    def test_direct_save_path_passes_confirm(self):
        src = _function_source("hp_scan_gui_pyside.py", "_direct_save_result")
        assert "confirm=" in src, (
            "直存路径没有把体积门禁的 confirm 回调接上 ⇒ "
            "超过 1.5GB 时用户看不到任何提示:\n%s" % src)

    def test_session_save_path_passes_confirm(self):
        src = _function_source("hp_scan_gui_pyside.py", "_save_session_now")
        assert "confirm=" in src, (
            "会话落盘路径没有把体积门禁的 confirm 回调接上 ⇒ "
            "超过 1.5GB 时用户看不到任何提示:\n%s" % src)

    def test_preview_path_still_passes_confirm(self):
        """★ 既有那条（预览对话框）不能被改坏。"""
        src = _function_source("hp_scan_gui_pyside.py", "_confirm",
                               class_name="MultiPagePreviewDialog")
        assert "confirm=" in src, "预览路径的 confirm 接线丢了"


# ── ③ 单页路径不得因 confirm 参数而 TypeError ────────────────────────────

class TestSinglePagePathAcceptsConfirm:
    """★ `save_single` 没有 confirm 参数 ⇒ 统一入口必须处理掉它。"""

    def test_save_page_specs_single_page_does_not_pass_confirm_to_save_single(self,
                                                                              tmp_path,
                                                                              monkeypatch):
        """单页走 `save_single`（**无** confirm 形参）⇒ 透传会 TypeError。"""
        captured = {}

        def _fake_save_single(cache_path, ext, output_path, **kwargs):
            captured.update(kwargs)
            return str(output_path)

        monkeypatch.setattr(gui.save_manager, "save_single", _fake_save_single)
        out = str(tmp_path / "single.jpg")
        gui._save_page_specs(
            "job_x", "jpg", out, [(str(tmp_path / "p.jpg"), 0)], [[0]],
            confirm=lambda msg: True)
        assert "confirm" not in captured, (
            "confirm 被透传给了 save_single（它没有这个形参）⇒ TypeError")

    def test_multi_page_path_still_passes_confirm_through(self, tmp_path, monkeypatch):
        """多页走 `save_pages`（**有** confirm 形参）⇒ 必须原样透传。"""
        captured = {}

        def _fake_save_pages(job_id, ext, output_path, page_specs, groups, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(gui.save_manager, "save_pages", _fake_save_pages)
        cb = lambda msg: True          # noqa: E731
        gui._save_page_specs(
            "job_y", "pdf", str(tmp_path / "multi.pdf"),
            [(str(tmp_path / "p1.jpg"), 0), (str(tmp_path / "p2.jpg"), 0)],
            [[0, 1]], confirm=cb)
        assert captured.get("confirm") is cb, (
            "多页路径的 confirm 没透传 ⇒ 体积门禁失效")
