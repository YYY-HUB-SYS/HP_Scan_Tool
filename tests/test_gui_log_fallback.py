"""
终审 Minor 修复守护用例 —— GUI 导入期的 Desktop 依赖（M-6）

被守护的行为
------------
1) **导入期不得因为 `~/Desktop` 不存在而崩**
   `setup_exception_logging()` 在**模块 import 时**执行，日志落在
   `%USERPROFILE%\\Desktop\\hp_scan_debug.log`。Desktop 被 OneDrive 重定向 /
   改名（中文 Windows 常见「桌面」目录）/ 域策略下发时，
   `logging.basicConfig(filename=...)` 抛 FileNotFoundError
   ⇒ **整个应用起不来**，而且报错指向日志路径而不是根因。

2) **正常情况下日志落点不许变**
   Desktop 存在时，日志仍必须落在
   `%USERPROFILE%\\Desktop\\hp_scan_debug.log`
   （这是刻意不污染真实 AppData 的选择）。

★ 必须是**子进程级**断言：pytest 进程里 `hp_scan_gui_pyside` 早就被
  别的用例 import 过 ⇒ 进程内再 import 不会重新跑 `setup_exception_logging()`。
  只有独立解释器 + 改过的 USERPROFILE 才能复现"导入期崩溃"。
"""

import json
import os
import pathlib
import subprocess
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 子进程探针：导入 GUI 模块 ⇒ 报告真正的日志落点
_PROBE_CODE = r"""
import json, logging, os
import hp_scan_gui_pyside as g
handlers = []
for h in logging.getLogger().handlers:
    base = getattr(h, "baseFilename", None)
    if base:
        handlers.append(base)
print(json.dumps({
    "handlers": handlers,
    "resolved": g._resolve_log_file(),
    "home": os.path.expanduser("~"),
    "desktop_exists": os.path.isdir(os.path.join(os.path.expanduser("~"), "Desktop")),
}))
"""


def _run_probe(userprofile, appdata):
    """在独立解释器里 import GUI 模块，返回 (CompletedProcess, 探针输出)"""
    env = os.environ.copy()
    env["USERPROFILE"] = str(userprofile)     # os.path.expanduser("~") 跟着它走
    env["HOME"] = str(userprofile)
    env["APPDATA"] = str(appdata)             # 用户数据隔离
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"       # 不弹窗、不抢焦点
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE_CODE],
        cwd=str(PROJECT_ROOT), env=env,
        capture_output=True, timeout=300,
    )
    payload = {}
    if proc.stdout:
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        if text:
            payload = json.loads(text.splitlines()[-1])
    return proc, payload


class TestGuiImportWithoutDesktop:
    def test_import_succeeds_when_desktop_missing(self, tmp_path):
        """★ 核心断言：没有 Desktop 目录时，import 不得抛 FileNotFoundError。"""
        home = tmp_path / "home_without_desktop"
        home.mkdir(parents=True)
        assert not (home / "Desktop").exists()

        proc, payload = _run_probe(home, tmp_path / "appdata")

        assert proc.returncode == 0, (
            "import 期崩溃（rc=%s）\nstdout=%r\nstderr=%r"
            % (proc.returncode,
               proc.stdout.decode("utf-8", "replace")[:2000],
               proc.stderr.decode("utf-8", "replace")[:4000]))
        assert payload, "探针没有输出（导入过程本身就不正常）"

    def test_log_file_lands_in_an_existing_writable_dir(self, tmp_path):
        """兜底后的日志目录必须**存在且可写**（不能指向不存在的 Desktop）。"""
        home = tmp_path / "home_without_desktop"
        home.mkdir(parents=True)

        proc, payload = _run_probe(home, tmp_path / "appdata")
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[:4000]

        resolved = payload.get("resolved")
        assert resolved, "没有解析出日志路径"

        log_dir = os.path.dirname(resolved)
        assert os.path.isdir(log_dir), (
            "兜底日志目录不存在：%s" % log_dir)
        assert os.access(log_dir, os.W_OK), (
            "兜底日志目录不可写：%s" % log_dir)
        assert os.path.exists(resolved), (
            "兜底日志文件没被创建：%s" % resolved)
        assert os.path.dirname(resolved) != os.path.join(
            payload["home"], "Desktop"), (
            "仍然指向不存在的 Desktop 目录")

    def test_no_bogus_desktop_folder_is_created(self, tmp_path):
        """不得为了写日志而**凭空造一个 Desktop 目录**（那不是用户真正的桌面）。"""
        home = tmp_path / "home_without_desktop"
        home.mkdir(parents=True)

        proc, _ = _run_probe(home, tmp_path / "appdata")
        assert proc.returncode == 0

        assert not (home / "Desktop").exists(), (
            "导入期凭空创建了 Desktop 目录")


class TestGuiLogLocationUnchanged:
    def test_normal_case_still_logs_to_desktop(self, tmp_path):
        """★ 正常情况（Desktop 存在）⇒ 日志落点不许变。"""
        home = tmp_path / "home_with_desktop"
        (home / "Desktop").mkdir(parents=True)

        proc, payload = _run_probe(home, tmp_path / "appdata")

        assert proc.returncode == 0, (
            "正常环境 import 失败（rc=%s）\n%s"
            % (proc.returncode,
               proc.stderr.decode("utf-8", "replace")[:4000]))
        expected = str(home / "Desktop" / "hp_scan_debug.log")
        assert payload["resolved"] == expected, (
            "正常情况下日志落点变了：%r != %r" % (payload["resolved"], expected))
        assert payload["handlers"] == [expected], (
            "logging 实际写到了别处：%r" % (payload["handlers"],))
        assert os.path.exists(expected), "日志文件没被创建：%s" % expected
