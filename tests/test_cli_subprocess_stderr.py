"""
CLI stderr 形态 —— **子进程级**回归测试（复审 N-1 / N-2）
=========================================================

N-1（P1）：真实 CLI 进程的 stderr 里，协议层写好的**可执行指引**后面
还跟着**一整份 Python traceback**（含本地绝对路径）。
根因：CLI 从不配置 logging ⇒ 兜底 handler `logging.lastResort` 把
`logger.exception(...)`（自带 `exc_info`）连同 traceback 写进了 stderr。

N-2（P2）：意外错误的 traceback 打了**两次**
（`cli.py` 里 `logger.exception(...)` 一次 + `traceback.print_exc(...)` 一次）。

★ 为什么这两条判据必须放**子进程**里
-------------------------------------
pytest 给 root logger 挂了 `LogCaptureHandler` ⇒ `logging.lastResort`
永不启用 ⇒ 进程内 capsys 看不到真实进程的形态 ⇒ 进程内断言 = 假绿
（复审 N-1 原文）。本文件起的是**真实 CLI 进程**
（`sys.executable -m cli ...`），logging 状态是干净的 ⇒ 能真正复现。
细节见同目录 `cli_subprocess_probe.py`。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cli_subprocess_probe  # noqa: E402


TRACEBACK_MARK = "Traceback (most recent call last)"


@pytest.fixture(scope="module")
def conflict_run(tmp_path_factory):
    """真实 CLI 进程 · 假设备回 409 ⇒ 协议层的可操作错误（含可执行指引）。"""
    appdata = tmp_path_factory.mktemp("appdata_conflict")
    return cli_subprocess_probe.run_cli(
        ["scan", "--ip", "127.0.0.1"], mode="conflict", appdata=appdata)


@pytest.fixture(scope="module")
def bug_run(tmp_path_factory):
    """真实 CLI 进程 · execute_scan 抛 ValueError ⇒ 意外错误。"""
    appdata = tmp_path_factory.mktemp("appdata_bug")
    return cli_subprocess_probe.run_cli(
        ["scan", "--ip", "127.0.0.1"], mode="bug", appdata=appdata)


class TestActionableErrorHasNoTracebackInRealProcess:
    """N-1：可操作错误 ⇒ stderr **只有**可执行指引，没有 traceback。"""

    def test_guidance_is_present(self, conflict_run):
        rc, out, err = conflict_run
        assert rc != 0, "失败退出码必须非 0"
        assert "打印机面板" in err, "必须告诉用户去打印机面板取消任务"
        assert "15-20 分钟" in err, "必须告诉用户设备回收要多久"

    def test_stderr_has_no_traceback(self, conflict_run):
        rc, out, err = conflict_run
        assert TRACEBACK_MARK not in err, (
            "真实 CLI 进程的 stderr 不得带 traceback（复审 N-1）"
            "；实际 stderr=%r" % err)
        assert "  File \"" not in err, (
            "不该把源码路径甩给用户；实际 stderr=%r" % err)

    def test_stdout_stays_empty(self, conflict_run):
        rc, out, err = conflict_run
        assert out == "", f"失败时 stdout 必须为空，实际 {out!r}"


class TestUnexpectedErrorTracebackPrintedOnce:
    """N-2：意外错误 ⇒ traceback 保留，但**只打一次**。"""

    def test_traceback_appears_exactly_once(self, bug_run):
        rc, out, err = bug_run
        assert rc != 0, "失败退出码必须非 0"
        assert err.count(TRACEBACK_MARK) == 1, (
            "意外错误的 traceback 只能打一次（复审 N-2），实际 %d 次；stderr=%r"
            % (err.count(TRACEBACK_MARK), err))

    def test_error_message_is_present(self, bug_run):
        rc, out, err = bug_run
        assert "某个内部变量是 None" in err, "必须告诉用户出了什么事"

    def test_stdout_stays_empty(self, bug_run):
        rc, out, err = bug_run
        assert out == "", f"失败时 stdout 必须为空，实际 {out!r}"
