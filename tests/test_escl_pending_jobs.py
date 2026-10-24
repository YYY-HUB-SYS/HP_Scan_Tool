"""
「设备侧没收尾的作业」清单契约测试 — HP Scan Tool v4.0

★ 2026-10-24 真机实测坐实（台账 §13.32）：**DELETE 不能再发**。
  DELETE 的语义是「**取消**设备上的作业」⇒ 设备把作业记成
  `JobStateReason=JobCanceledByUser` ⇒ **M232 机型上扫描子系统被永久锁死**
  （`POST /ScanJobs` 一律 409；客户端再 DELETE 只得 404/410 ⇒ 只有断电能清）。
  ⇒ `.203` / `.208` / `.111` 那三条 Canceled 残留就是这么来的。

★ 而 DELETE **本来也没用**：文档一取走，设备自己就把作业记成 `Completed`
  并回收（`.101` 实测：随后发出的 DELETE 拿到的是 404）。

★ 所以清单的语义变成：
  - 收尾时**取不到确定答复**（503 / 超时 / 异常）⇒ 记进清单；
  - 下一轮建 job **之前**先把文档取走（`cleanup_pending_jobs`）⇒
    取到了 / 设备说没有更多页 ⇒ 从清单移除；
  - **任何情况下都不发 DELETE**。
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
    """记录 GET / DELETE 的假 session

    ★ 2026-10-24：产品**不再发 DELETE**了（DELETE = 取消 = 锁死设备）。
      这里保留 `deleted` 列表，就是为了让守护测试能断言
      「产品一次 DELETE 都没发过」。
    """

    def __init__(self, results=None, raise_on=()):
        self.results = list(results or [])
        self.raise_on = set(raise_on)
        self.deleted = []
        self.gets = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        if url in self.raise_on:
            raise RuntimeError("设备不回应（读超时）")
        return _Resp(404)

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
    def test_resolved_entry_removes_from_list(self):
        """设备给了确定答复（404 = 没有更多页）⇒ 条目移除。"""
        escl_engine.remember_pending_job("u1")
        s = _Session([200])
        done = escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/")
        assert done == 1
        assert escl_engine.load_pending_jobs() == []
        assert s.deleted == [], \
            "★ 收尾绝不能发 DELETE（取消设备上的作业 ⇒ M232 永久锁死）"

    def test_404_also_removes(self):
        """404 = 设备已回收 ⇒ 留着只是噪音 ⇒ 也要移除"""
        escl_engine.remember_pending_job("u1")
        s = _Session([404])
        escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/")
        assert escl_engine.load_pending_jobs() == []
        assert s.deleted == []

    def test_device_silent_keeps_it_for_next_round(self):
        """★ 设备不回应 ⇒ **保留**在清单里，下次再试（绝不能改用 DELETE）。"""
        escl_engine.remember_pending_job("u1")
        # 设备的 JobUri 是相对主机根的 ⇒ 绝对 URL = origin + "/u1"
        s = _Session([200], raise_on=("http://1.2.3.4/u1/NextDocument",))
        done = escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/")
        assert done == 0
        assert escl_engine.load_pending_jobs() == ["u1"]
        assert s.deleted == []

    def test_relative_uri_is_made_absolute(self):
        """★ 相对 URI ⇒ 拼成 origin + 路径（**不能**拼成 /eSCL/eSCL/...）"""
        escl_engine.remember_pending_job("/eSCL/ScanJobs/xyz")
        s = _Session([200])
        escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/")
        assert s.gets == ["http://1.2.3.4/eSCL/ScanJobs/xyz/NextDocument"], s.gets
        assert s.deleted == [], s.deleted

    def test_empty_list_is_noop(self):
        s = _Session()
        assert escl_engine.cleanup_pending_jobs(s, "http://1.2.3.4/eSCL/") == 0
        assert s.deleted == []


class TestCreateJobCleansUpFirst:
    """★ 建 job **之前**必须先收尾残留"""

    def test_create_scan_job_cleans_pending_first(self):
        import inspect
        src = inspect.getsource(escl_engine._create_scan_job)
        assert "cleanup_pending_jobs" in src, \
            "建 job 之前没有先收尾残留 ⇒ 设备会拒接新任务（409）"

    def test_unresolved_job_is_remembered(self):
        import inspect
        src = inspect.getsource(escl_engine.execute_scan)
        assert "remember_pending_job" in src, \
            "没收尾的作业没有记进清单 ⇒ 下次没法补取"
        src2 = inspect.getsource(escl_engine.execute_multipage_scan)
        assert "remember_pending_job" in src2, \
            "多页路径没收尾的作业也没有记进清单"
