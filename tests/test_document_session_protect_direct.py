"""
终审 Minor 修复守护用例 —— document_session（M-4 过期注释 + 死分支）

被守护的行为
------------
1) **保护 / 解除保护的既有行为完全不变**
   `cache_manager.protect_dir` / `unprotect_dir` **早已落地**
   （cache_manager.py:305 / :326）⇒ 以前那句
   「B4-2 落地后这段代码自动生效」是**过期信息**，
   `getattr(cache_manager, "protect_dir", None)` 的存在性检查是**死分支**。
   修掉死分支之后，行为必须一模一样：
   - 每个 job_id 都会被登记保护 / 解除保护；
   - 保护失败（抛异常）只记日志，**不得**把异常抛给调用方。

2) **死分支不得复活**
   模块里不允许再出现 `getattr(cache_manager, "protect_dir", None)`
   这种"函数不存在就静默跳过"的写法 —— 缺失应当立刻暴露，
   而不是悄悄不保护（那会让会话页被清理任务删掉）。

★ 非空转验证：第 1 条盯住**可观测行为**（保护集合 / 异常吞吐）；
  第 2 条盯住**源码事实**（死分支字面量）。
"""

import inspect
import os

import pytest

import cache_manager
import document_session


@pytest.fixture
def clean_protection(monkeypatch):
    """把进程内保护集合换成干净的，并保证测试后不残留。"""
    monkeypatch.setattr(cache_manager, "_protected_dirs", {}, raising=False)
    return cache_manager._protected_dirs


class TestProtectUnprotectBehaviorUnchanged:
    def test_protect_jobs_registers_every_job(self, clean_protection):
        """★ 每个 job_id 都要被登记保护。"""
        document_session._protect_jobs(["jobA", "jobB"])

        assert "jobA" in clean_protection
        assert "jobB" in clean_protection
        assert cache_manager.is_job_protected("jobA")
        assert cache_manager.is_job_protected("jobB")

    def test_unprotect_jobs_removes_every_job(self, clean_protection):
        document_session._protect_jobs(["jobA", "jobB"])
        document_session._unprotect_jobs(["jobA", "jobB"])

        assert "jobA" not in clean_protection
        assert "jobB" not in clean_protection
        assert not cache_manager.is_job_protected("jobA")

    def test_protect_failure_is_swallowed(self, clean_protection, monkeypatch):
        """★ 保护失败只记日志，不得把异常抛出去（调用方是会话落盘主路径）。"""
        def boom(name):
            raise RuntimeError("protect failed")

        monkeypatch.setattr(cache_manager, "protect_dir", boom)

        document_session._protect_jobs(["jobA"])      # 不得抛异常

    def test_unprotect_failure_is_swallowed(self, clean_protection, monkeypatch):
        def boom(name):
            raise RuntimeError("unprotect failed")

        monkeypatch.setattr(cache_manager, "unprotect_dir", boom)

        document_session._unprotect_jobs(["jobA"])    # 不得抛异常

    def test_protect_jobs_accepts_empty_input(self, clean_protection):
        document_session._protect_jobs([])
        document_session._unprotect_jobs([])
        assert clean_protection == {}


class TestDeadBranchRemoved:
    """死分支（`getattr(..., None)` 存在性检查）必须消失"""

    @staticmethod
    def _source():
        return inspect.getsource(document_session)

    def test_no_getattr_existence_check_left(self):
        src = self._source()
        assert 'getattr(cache_manager, "protect_dir"' not in src, (
            "死分支又回来了：cache_manager.protect_dir 早已落地，不需要存在性检查")
        assert 'getattr(cache_manager, "unprotect_dir"' not in src, (
            "死分支又回来了：cache_manager.unprotect_dir 早已落地，不需要存在性检查")

    def test_direct_calls_are_used(self):
        src = self._source()
        assert "cache_manager.protect_dir(job_id)" in src
        assert "cache_manager.unprotect_dir(job_id)" in src

    def test_stale_b4_2_note_is_gone(self):
        """「B4-2 落地后这段代码自动生效」是过期信息，必须删掉。"""
        src = self._source()
        assert "B4-2" not in src, (
            "模块里还留着「B4-2 落地后自动生效」的过期说明 —— 读代码的人会误判"
            "缓存目录保护还没做")
        assert "尚未落地" not in src
        assert "并行任务" not in src


class TestNoQtDependency:
    """document_session 是纯 Python 模块（CLI 也要能用）"""

    def test_module_does_not_import_qt(self):
        src = inspect.getsource(document_session)
        assert "PySide6" not in src
        assert "import tkinter" not in src
        assert os.path.basename(document_session.__file__) == "document_session.py"
