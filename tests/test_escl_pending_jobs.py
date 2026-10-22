"""
「设备侧没删掉的作业」清单契约测试 — HP Scan Tool v4.0

★ 治本的那一条（2026-10-22）：DELETE 不回包时**记下来**，
  **下次建 job 之前**先补删 ⇒ 设备不会因为「上次那个作业没收尾」而拒接新任务。

★ 为什么必须有（实测 + 公网证据）：
  · NAPS2 `EsclScanDriver.cs:213-217`：「Some devices (e.g. Kyocera) **stay busy
    until the completed job is deleted** … so we always send DELETE for the job」；
    NAPS2 issue #906：「删掉已完成的 job 后设备才解锁」。
  · 我们实测：设备在处理中时 DELETE **长时间不返回**（6 秒读超时）⇒ 3 秒就放弃
    ⇒ 作业停在没收尾状态 ⇒ 设备侧记 Canceled/Aborted ⇒ **那台打印机从此 409**，
    客户端再 DELETE 只会 404/410（清不掉）⇒ 只能重启打印机。
    ⇒ 2026-10-22 实测 `.208` 的两条 Canceled Age=2.0 天、`.111` 的 1.0 天
      —— **就是这条路把三台打印机搞成扫不了的。**
"""

import json
import os

import pytest

import escl_engine


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = ""
        self.headers = {}


class _Session:
    """记录 DELETE 的假 session"""

    def __init__(self, results=None, raise_on=()):
        self.results = list(results or [])
        self.raise_on = set(raise_on)
        self.deleted = []

    def delete(self, url, timeout=None):
        self.deleted.append(url)
        if url in self.raise_on:
            raise RuntimeError("设备不回应（读超时）")
        code = self.results.pop(0) if self.results else 200
        return _Resp(code)


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path, monkeypatch):
    """清单文件落在临时 APPDATA ⇒ 不碰真实用户目录"""
    appdata = tmp_path / "appdata"
    (appdata / "HP_Scan_Tool").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    yield appdata


class TestPendingJobsList:
    def test_remember_persists_to_disk(self):
        escl_engine.remember_pending_job("http://1.2.3.4/eSCL/ScanJobs/abc")
        path = escl_engine._pending_jobs_path()
        assert os.path.isfile(path), "清单没落盘"
        data = json.loads(open(path, encoding="utf-8").read())
        assert "http://1.2.3.4/eSCL/ScanJobs/abc" in data

    def test_list_survives_process_restart(self):
        """跨进程：重新 load 仍然在（靠的就是落盘，不是内存）"""
        escl_engine.remember_pending_job("http://1.2.3.4/eSCL/ScanJobs/abc")
        assert "http://1.2.3.4/eSCL/ScanJobs/abc" in escl_engine.load_pending_jobs()

    def test_forget_removes(self):
        escl_engine.remember_pending_job("u1")
        escl_engine.remember_pending_job("u2")
        escl_engine.forget_pending_job("u1")
        assert escl_engine.load_pending_jobs() == ["u2"]

    def test_corrupt_file_is_tolerated(self):
        path = escl_engine._pending_jobs_path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        assert escl_engine.load_pending_jobs() == []


class TestCleanupPendingJobs:
    def test_successful_delete_removes_from_list(self):
        escl_engine.remember_pending_job("u1")
        s = _Session([200])
        done = escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/")
        assert done == 1
        assert escl_engine.load_pending_jobs() == []

    def test_404_also_removes(self):
        """404 = 设备已回收 ⇒ 留着只是噪音 ⇒ 也要移除"""
        escl_engine.remember_pending_job("u1")
        s = _Session([404])
        escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/")
        assert escl_engine.load_pending_jobs() == []

    def test_timeout_keeps_it_for_next_round(self):
        """★ 设备不回应 ⇒ **保留**在清单里，下次再试"""
        escl_engine.remember_pending_job("u1")
        # 设备的 JobUri 是相对主机根的 ⇒ 绝对 URL = origin + "/u1"
        s = _Session([200], raise_on=("http://1.2.3.4/u1",))
        done = escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/")
        assert done == 0
        assert escl_engine.load_pending_jobs() == ["u1"]

    def test_relative_uri_is_made_absolute(self):
        """★ 相对 URI ⇒ 拼成 origin + 路径（**不能**拼成 /eSCL/eSCL/...）"""
        escl_engine.remember_pending_job("/eSCL/ScanJobs/xyz")
        s = _Session([200])
        escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/")
        assert s.deleted == ["http://1.2.3.4/eSCL/ScanJobs/xyz"], s.deleted

    def test_empty_list_is_noop(self):
        s = _Session()
        assert escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/") == 0
        assert s.deleted == []


class TestCreateJobCleansUpFirst:
    """★ 建 job **之前**必须先补删残留"""

    def test_create_scan_job_cleans_pending_first(self):
        import inspect
        src = inspect.getsource(escl_engine._create_scan_job)
        assert "cleanup_pending_jobs" in src, \
            "建 job 之前没有补删残留 ⇒ 设备会拒接新任务（409）"

    def test_delete_failure_is_remembered(self):
        import inspect
        src = inspect.getsource(escl_engine.execute_scan)
        assert "remember_pending_job" in src, \
            "DELETE 失败没有记进清单 ⇒ 下次没法补删"
        src2 = inspect.getsource(escl_engine.execute_multipage_scan)
        assert "remember_pending_job" in src2, \
            "多页路径的 DELETE 失败也没有记进清单"
