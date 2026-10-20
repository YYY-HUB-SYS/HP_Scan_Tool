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
   当进程里只有 QCoreApplication 时（实测：`type(x).__name__` 仍是
   `QCoreApplication`、`isinstance(x, QApplication)` 为 **False**），
   `QApplication.instance()` 返回的**就是那个 QCoreApplication**
   （**非 None**）⇒ `or` 短路 ⇒ **QApplication 从头到尾没被创建过**。
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
        "原样返回（非 None），`or` 因此短路，此后创建的 widget 必然崩"
        % type(core).__name__
    )
    assert QApplication.instance() is core


def test_no_test_module_instantiates_qcoreapplication():
    """
    任何测试模块都不得**自己 new 一个 QCoreApplication**。

    实测：function 级作用域的 QCoreApplication **并不会被真正析构**
    （`del` + `gc.collect()` 之后 `QCoreApplication.instance()` 依然返回它
    —— PySide6 一直持有）⇒ 它会**长期占住** application 的位置，
    后续 `QApplication.instance()` 每次都取到它。

    ★★ 用 **AST 判据**而不是正则（复审 I2 —— 旧判据是**假网**）
    ------------------------------------------------------------------
    旧实现用正则 `QCoreApplication\\s*\\(\\s*\\[` ⇒ 只认**列表字面量**实参，
    于是 `QCoreApplication(sys.argv)` / `QCoreApplication(argv)` /
    `QCoreApplication()` **全部漏网**。
    复审的变异测试 M4 实测：把某个 fixture 改成 `QCoreApplication(sys.argv)`
    ⇒ 守护**仍然 4 passed**（假绿），而全量测试在 ~58% 处 **0xC0000409 硬崩**
    ⇒ 这条守护当时**抓不住**它要防的东西。
    ⇒ 现在改成：AST 里任何对 `QCoreApplication` 的**调用**（`ast.Call`）都算违规；
      `QCoreApplication.instance()` 是**属性访问**（读已有实例），不算调用。
    """
    offenders = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "QCoreApplication":
                offenders.append("%s:%d" % (path.name, node.lineno))
                break
    assert offenders == [], (
        "这些测试文件自己 new 了 QCoreApplication（PySide6 不会释放它 ⇒ "
        "它会长期占住 application 的位置；任何实参形式都算违规）：%s"
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


def test_widget_creation_without_requesting_qt_app_does_not_crash():
    """
    ★ 回归 I3：**不请求** `qt_app` 的用例直接创建 widget，也**不得**硬崩。

    旧实现只在"用例**显式请求** fixture"时才有 QApplication ⇒ 复审的最小复现
    （当前进程里直接建 widget）仍是 0xC0000409 硬崩 ⇒ 守护网有缺口。
    ⇒ 这里跑一个**子进程 pytest**，它只含一个**不请求任何 fixture**的用例
      （直接创建 QCheckBox），断言退出码为 0
      ⇒ 证明会话级保护对"不请求"的用例同样生效。

    ★ 为什么探针文件放在 `tests/` 目录里、且由**子进程自己**创建：
      - pytest 按**目录层级**加载 conftest ⇒ 探针必须在 `tests/` 下才会
        加载到 `tests/conftest.py`（用 `-p conftest` 插件方式加载会让
        conftest 被加载**两次**，导入期建的沙箱目录互相打架 ⇒ 报
        FileNotFoundError，本轮实测踩过）。
      - 文件**由子进程创建**（父进程的收集阶段早已结束）⇒ 并行跑的其它
        pytest 不会在收集阶段把它捡走 ⇒ 不会给别的测试制造假失败。
      - 名字以 `_tmp_` 开头 ⇒ 不匹配 `test_*.py` ⇒ 默认收集不会碰它。
      - 子进程 finally 里删掉 ⇒ 仓库目录不残留。
    """
    import os
    import subprocess
    import sys
    import tempfile

    repo_root = str(TESTS_DIR.parent)
    maker = (
        "import os, sys, subprocess\n"
        "tests_dir = sys.argv[1]\n"
        "repo_root = sys.argv[2]\n"
        "probe = os.path.join(tests_dir, '_tmp_qapp_probe_%d.py' % os.getpid())\n"
        "with open(probe, 'w', encoding='utf-8') as f:\n"
        "    f.write('from PySide6.QtWidgets import QCheckBox\\n\\n')\n"
        "    f.write('def test_widget_without_requesting_any_fixture():\\n')\n"
        "    f.write(\"    cb = QCheckBox('x')\\n\")\n"
        "    f.write('    cb.setChecked(True)\\n')\n"
        "    f.write('    assert cb.isChecked()\\n')\n"
        "rc = 1\n"
        "try:\n"
        "    rc = subprocess.call([sys.executable, '-m', 'pytest', '-q',\n"
        "        '--no-header', '-p', 'no:cacheprovider', probe], cwd=repo_root)\n"
        "finally:\n"
        "    try:\n"
        "        os.remove(probe)\n"
        "    except OSError:\n"
        "        pass\n"
        "sys.exit(rc)\n"
    )
    tmp_dir = tempfile.mkdtemp(prefix="hp_qapp_maker_")
    maker_path = os.path.join(tmp_dir, "maker.py")
    with open(maker_path, "w", encoding="utf-8") as f:
        f.write(maker)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        proc = subprocess.run(
            [sys.executable, maker_path, str(TESTS_DIR), repo_root],
            cwd=repo_root, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
    finally:
        try:
            os.remove(maker_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass
    assert proc.returncode == 0, (
        "不请求任何 fixture 的用例创建 widget 仍然崩了（exit=%s）:\n%s"
        % (proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))[-2000:]))
