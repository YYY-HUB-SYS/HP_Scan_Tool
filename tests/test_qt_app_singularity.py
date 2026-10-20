"""
Qt application 实例唯一性守护 — 回归 2026-10-21 的 0xC0000409 崩溃。

★ 缺陷背景（实测复现，非推断）
--------------------------------------------------------------------------
2026-10-21 凌晨发现：只跑两个测试文件
    `pytest tests/test_scan_coordinator.py tests/test_gui_consistency.py`
⇒ 44 个点后进程**硬崩**，退出码 `0xC0000409`（STATUS_STACK_BUFFER_OVERRUN），
pytest 连 traceback 都来不及打。单独跑任一文件都绿 ⇒ **跨文件状态污染**。

根因（已用独立探针 `%TEMP%\\hp_qapp_probe.py` 原样复现）：

1. `tests/test_scan_coordinator.py` 的 `qt_app` fixture 建的是 **QCoreApplication**，
   且是 **function 级作用域** ⇒ 每个用例结束就把它**析构**掉。
2. `tests/test_gui_consistency.py` 的 `qapp` fixture 写的是
   `QApplication.instance() or QApplication([])`。
   当进程里只剩一个**已析构的 QCoreApplication** 时，
   `QApplication.instance()` 返回的是**野指针**（非 None）⇒ `or` 短路
   ⇒ **QApplication 从头到尾没被创建过**。
3. 下一个用例创建 `QCheckBox` ⇒ Qt 报
   `QWidget: Cannot create a QWidget without QApplication`
   ⇒ PySide6 在 C++ 侧 abort ⇒ 进程 0xC0000409 退出。

★ 为什么「全量 985 passed」没抓到
--------------------------------------------------------------------------
全量跑时按文件名字母序，别的 GUI 测试文件**先**建好了 QApplication，
第 2 步的 `QApplication.instance()` 于是返回**合法**实例 ⇒ 缺陷被掩盖。
**「全量绿」在这里是假绿** —— 这正是本项目要根治的「顺序依赖型假绿」。

★ 修复方向
--------------------------------------------------------------------------
进程内**只允许存在一个 application 实例**，且必须是 **QApplication**：

- `tests/conftest.py` 提供 **session 级** `qt_app` fixture：整个 pytest 进程
  只建一次 QApplication（offscreen），**永不销毁**。
- 任何测试文件都**不得**自己 `QCoreApplication(...)`（function 级 ⇒ 用完即析构）。
- 任何测试文件里的模块级 `qapp` fixture 都必须**依赖会话级 `qt_app`**，
  而不是各自 `QApplication.instance() or QApplication([])`。
"""

import ast
import os
import pathlib

import pytest

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TESTS_DIR = pathlib.Path(__file__).resolve().parent


def test_process_holds_exactly_one_qapplication(qt_app):
    """
    进程内唯一的 application 实例必须**就是** QApplication。

    断言的是真实运行状态（QCoreApplication.instance() 的实际类型），
    不是 mock 行为 —— 这正是原缺陷发作的位置。
    """
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    core = QCoreApplication.instance()
    assert core is not None, "进程里必须有一个 application 实例（会话级 qt_app 应已创建）"
    assert isinstance(core, QApplication), (
        "application 实例是 %s 而不是 QApplication —— QCoreApplication 与 "
        "QApplication 不能混用：QApplication.instance() 会把 QCoreApplication "
        "当成 QApplication 返回（野指针），此后创建的 widget 必然崩"
        % type(core).__name__
    )
    assert QApplication.instance() is core


def test_no_test_module_instantiates_qcoreapplication():
    """
    任何测试模块都不得**自己 new 一个 QCoreApplication**。

    function 级作用域的 QCoreApplication 会在用例结束时被析构，
    留下的野指针会被后续 `QApplication.instance()` 当成合法实例取走。
    """
    import re

    offenders = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"QCoreApplication\s*\(\s*\[", text):
            offenders.append(path.name)
    assert offenders == [], (
        "这些测试文件自己 new 了 QCoreApplication（用完即析构 ⇒ 留下野指针）：%s"
        % offenders
    )


def test_qt_app_fixture_is_session_scoped():
    """
    conftest 的 `qt_app` 必须是 **session 级**。

    function 级会在每个用例后销毁 application ⇒ 后续模块拿到已析构的实例
    ⇒ 复现本次 0xC0000409。
    """
    import conftest

    # pytest 9 用 `_fixture_function_marker`，pytest 8 及更早用
    # `_pytestfixturefunction` —— 两种都认，避免测试被 pytest 版本绑死。
    marker = (getattr(conftest.qt_app, "_fixture_function_marker", None)
              or getattr(conftest.qt_app, "_pytestfixturefunction", None))
    assert marker is not None, "tests/conftest.py 必须定义 qt_app fixture"
    assert getattr(marker, "scope", None) == "session", (
        "qt_app 必须是 session 级（当前是 %r）—— function 级会在每个用例后"
        "销毁 application" % getattr(marker, "scope", None)
    )


def test_module_qapp_fixtures_reuse_session_instance():
    """
    各测试文件里的模块级 `qapp` fixture 必须**依赖会话级 `qt_app`**。

    自己写 `QApplication.instance() or QApplication([])` 的模块，
    在「会话实例尚未建立」时会自己另建一个 ⇒ 进程里出现第二个 application
    ⇒ 与本次缺陷同源。
    """
    offenders = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "qapp":
                args = {a.arg for a in node.args.args}
                if "qt_app" not in args:
                    offenders.append(path.name)
                    break
    assert offenders == [], (
        "这些文件的 qapp fixture 没有复用会话级 qt_app：%s" % offenders
    )
