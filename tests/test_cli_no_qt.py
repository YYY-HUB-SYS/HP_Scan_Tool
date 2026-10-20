"""
CLI **不背 Qt 依赖** 守护用例 — HP Scan Tool v4.0
覆盖链号 15.10：矩阵里 v1.0 记的 ✔ 是**误判**（当时并没有守护用例）。

被守护的行为
------------
`cli.py` 是**纯命令行**入口，它的导入链是：
    cli → escl_engine / wsd_engine / history_manager / save_manager
这些模块都**不** import PySide6，也**不** import GUI / 协调器
（仓库里只有 hp_scan_gui_pyside.py 与 scan_coordinator.py 碰 Qt）。

⇒ 导入 cli **不得**把 Qt 拖进进程：
   - CLI 可能跑在没有显示服务的机器 / SSH 会话里，import PySide6 会连带
     初始化 Qt 平台插件（xcb / wayland 缺失 ⇒ 进程启动即崩）；
   - import PySide6 本身要几百毫秒，CLI 的 discover 命令白等。

★ 必须是**子进程级**断言：
  当前 pytest 进程里 conftest 已经 import 了 PySide6（会话级 QApplication
  单例 fixture）⇒ 进程内的 `sys.modules` **一定**含有 'PySide6'
  ⇒ 进程内断言必然假红 / 假绿。

★ 非空转验证的前提：`_control` 那条用例证明**探测器本身是有效的**
  （故意 import PySide6 ⇒ 必须被报出来），否则"没报出来"可能是探测器坏了。
"""

import ast
import json
import os
import pathlib
import subprocess
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 导入 cli 之后**不允许**出现在 sys.modules 里的模块
FORBIDDEN_MODULES = ("scan_coordinator", "hp_scan_gui_pyside", "PySide6")

# 子进程探针：导入 cli ⇒ 报告命中的违禁模块（命中则退出码 3）
_PROBE_CODE = r"""
import json, sys
import cli
forbidden = ("scan_coordinator", "hp_scan_gui_pyside", "PySide6")
hit = [m for m in forbidden if m in sys.modules]
print(json.dumps(hit))
sys.exit(3 if hit else 0)
"""

# 对照探针：**故意**导入 PySide6 ⇒ 探测器必须能报出来（证明探测器有效）
_CONTROL_CODE = r"""
import json, sys
import PySide6
forbidden = ("scan_coordinator", "hp_scan_gui_pyside", "PySide6")
hit = [m for m in forbidden if m in sys.modules]
print(json.dumps(hit))
"""


def _run_subprocess(code, appdata):
    """在**独立解释器**里跑一段代码，返回 CompletedProcess。"""
    env = os.environ.copy()
    env["APPDATA"] = str(appdata)          # 用户数据隔离（子进程也隔离）
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"    # 万一真的拖进来 Qt，也不弹窗
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT), env=env,
        capture_output=True, timeout=120,
    )


class TestCliDoesNotPullQt:
    """子进程级断言：import cli 之后不得出现 Qt / GUI / 协调器模块。"""

    def test_import_cli_does_not_load_qt_modules(self, tmp_path):
        """★ 核心断言：导入 cli 后 sys.modules 里没有 Qt / GUI / 协调器。"""
        proc = _run_subprocess(_PROBE_CODE, tmp_path / "appdata")

        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        stderr = proc.stderr.decode("utf-8", errors="replace")
        assert proc.returncode == 0, (
            "子进程退出码 %d（stdout=%r, stderr=%r）—— import cli "
            "把不该有的模块拖进了进程，或 cli 本身导入失败"
            % (proc.returncode, stdout, stderr[:2000]))
        hit = json.loads(stdout)
        assert hit == [], "import cli 之后 sys.modules 里出现了：%r" % (hit,)

    def test_detector_is_not_vacuous(self, tmp_path):
        """★ 对照组：探测器必须**能**报出违禁模块（否则上面那条是假绿）。"""
        proc = _run_subprocess(_CONTROL_CODE, tmp_path / "appdata")

        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        stderr = proc.stderr.decode("utf-8", errors="replace")
        assert proc.returncode == 0, (
            "对照组子进程异常退出：rc=%s stderr=%r" % (proc.returncode, stderr[:2000]))
        hit = json.loads(stdout)
        assert hit == ["PySide6"], (
            "探测器失效：故意 import PySide6 却只报出 %r" % (hit,))

    def test_cli_import_still_works(self, tmp_path):
        """对照：cli 本身**能**被导入（否则上面的断言可能只是"导入失败"）。"""
        proc = _run_subprocess(
            "import cli; print(cli.__file__)", tmp_path / "appdata")

        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        stderr = proc.stderr.decode("utf-8", errors="replace")
        assert proc.returncode == 0, (
            "import cli 失败：rc=%s stderr=%r" % (proc.returncode, stderr[:2000]))
        assert stdout.endswith("cli.py"), "cli 导入路径异常：%r" % stdout


class TestCliSourceHasNoQtImport:
    """★ 源码级（AST）判据：cli.py 里不允许出现 Qt / GUI / 协调器的 import。

    子进程断言只看**运行时**的 sys.modules；AST 判据能在**导入发生之前**
    就把违规拦住（例如有人把 `import scan_coordinator` 写在函数体里，
    只有那条分支被执行时才会进 sys.modules ⇒ 运行期断言抓不到）。
    """

    @staticmethod
    def _imported_top_level_modules(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
        return names

    def test_cli_py_imports_no_qt_or_gui_modules(self):
        cli_py = PROJECT_ROOT / "cli.py"
        imported = self._imported_top_level_modules(cli_py)

        violations = sorted(set(imported) & set(FORBIDDEN_MODULES))
        assert violations == [], (
            "cli.py 里有对 Qt / GUI / 协调器的 import：%r" % (violations,))

    def test_cli_py_import_chain_stays_console_only(self):
        """cli.py 直接 import 的模块清单（守护"CLI 依赖链不许长出 Qt 分支"）。"""
        imported = self._imported_top_level_modules(PROJECT_ROOT / "cli.py")

        assert "escl_engine" in imported
        assert "wsd_engine" in imported
        assert "history_manager" in imported
        assert "save_manager" in imported
        assert "PySide6" not in imported
        assert "tkinter" not in imported
