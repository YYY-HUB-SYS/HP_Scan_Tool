"""收尾契约：**先把文档取走，再删作业** — HP Scan Tool v4.0

★ 为什么必须有这一步（2026-10-23 真机实测，详见台账 §13.23）：
  设备在 POST 之后会**自己扫好一页、把它攥在手里**
  （ScannerStatus 里表现为 ``Processing`` + ``ImagesCompleted=1``）。
  客户端如果**不取走文档就直接 DELETE** ⇒ 设备端的「忙」标志**解不开**：
  实测 DELETE 返回 **200**，设备**仍然 409** 拒接新作业
  ⇒ 那台打印机在很长一段时间里扫不了东西。
  ⇒ 收尾顺序必须是：**NextDocument 取走文档 → 再 DELETE**。

★ 本文件守的是**顺序**，不是"删了没删"：
  - 失败/取消/无文档（没拿到过文档）⇒ **必须补取**再删；
  - 已经拿到过文档（成功路径）⇒ **不必**再取（再取只会 404，白花时间）。
"""

import os

import pytest

import escl_engine


class _Resp:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content
        self.text = ""
        self.headers = {}


class _FakeSession:
    """记录 (GET/DELETE) 事件的假 session"""

    def __init__(self, events, get_results=None, delete_codes=None):
        self.events = events
        self.get_results = list(get_results or [])
        self.delete_codes = list(delete_codes or [])

    def get(self, url, timeout=None, **kw):
        self.events.append(("GET", url))
        if self.get_results:
            return self.get_results.pop(0)
        return _Resp(404)

    def delete(self, url, timeout=None, **kw):
        self.events.append(("DELETE", url))
        code = self.delete_codes.pop(0) if self.delete_codes else 200
        return _Resp(code)

    def post(self, *a, **kw):
        self.events.append(("POST", a[0] if a else ""))
        return _Resp(201)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    (appdata / "HP_Scan_Tool").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    yield appdata


def _scanner():
    return escl_engine.ScannerInfo(name="t", ip="10.0.0.9",
                                   escl_url="http://10.0.0.9/eSCL")


def _install_fakes(monkeypatch, events, get_results=None, delete_codes=None):
    fake = _FakeSession(events, get_results, delete_codes)
    monkeypatch.setattr(escl_engine.requests, "Session", lambda *a, **k: fake)
    monkeypatch.setattr(
        escl_engine, "_create_scan_job",
        lambda *a, **k: ("http://10.0.0.9/eSCL",
                         "http://10.0.0.9/eSCL/ScanJobs/j1", "jpg"))
    return fake


def _install_drain_recorder(monkeypatch, events):
    calls = []

    def _fake_drain(session, job_uri, max_gets=2, timeout=5.0):
        calls.append(job_uri)
        events.append(("DRAIN", job_uri))
        return 0

    monkeypatch.setattr(escl_engine, "_drain_job", _fake_drain)
    return calls


class TestDrainHelper:
    def test_200_keeps_collecting_pages(self):
        sess = _FakeSession([], get_results=[_Resp(200, b"abc"), _Resp(200, b"de")])
        total = escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j")
        assert total == 5
        assert len(sess.events) == 2, "应当继续取下一页"

    def test_404_stops(self):
        sess = _FakeSession([], get_results=[_Resp(404)])
        assert escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j") == 0
        assert len(sess.events) == 1

    def test_410_stops(self):
        sess = _FakeSession([], get_results=[_Resp(410)])
        assert escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j") == 0
        assert len(sess.events) == 1

    def test_503_stops_without_spinning(self):
        sess = _FakeSession([], get_results=[_Resp(503)])
        assert escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j") == 0
        assert len(sess.events) == 1, "503 不能死磕"

    def test_exception_is_swallowed(self):
        class _Boom:
            def get(self, *a, **k):
                raise RuntimeError("设备不回应")

        assert escl_engine._drain_job(_Boom(), "http://h/eSCL/ScanJobs/j") == 0

    def test_empty_uri_is_noop(self):
        sess = _FakeSession([])
        assert escl_engine._drain_job(sess, "") == 0
        assert sess.events == []

    def test_uses_nextdocument_url(self):
        sess = _FakeSession([], get_results=[_Resp(404)])
        escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j/")
        assert sess.events[0][1] == "http://h/eSCL/ScanJobs/j/NextDocument"


class TestExecuteScanCleanup:
    def test_no_document_must_drain_before_delete(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(404), _Resp(404)])
        _install_drain_recorder(monkeypatch, events)
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_scan(_scanner(), timeout=0.01)
        kinds = [e[0] for e in events]
        assert "DRAIN" in kinds, "没拿到文档 ⇒ 必须补取文档"
        assert kinds.index("DRAIN") < kinds.index("DELETE"), \
            "顺序错：必须先取文档再删作业"

    def test_success_path_does_not_drain_again(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(200, b"jpgdata")])
        _install_drain_recorder(monkeypatch, events)
        data, ext = escl_engine.execute_scan(_scanner(), timeout=5.0)
        assert data == b"jpgdata"
        kinds = [e[0] for e in events]
        assert "DRAIN" not in kinds, "已经拿到过文档 ⇒ 不必再取（再取只会 404）"
        assert "DELETE" in kinds, "收尾仍然必须删作业"

    def test_delete_still_happens_after_drain(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(404)])
        _install_drain_recorder(monkeypatch, events)
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_scan(_scanner(), timeout=0.01)
        deletes = [e for e in events if e[0] == "DELETE"]
        assert deletes, "取完文档照样要删作业"
        assert deletes[0][1].endswith("/eSCL/ScanJobs/j1")


class TestMultipageCleanup:
    def test_no_pages_must_drain(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(404)])
        _install_drain_recorder(monkeypatch, events)
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_multipage_scan(_scanner(), timeout=0.01, page_timeout=0.01)
        kinds = [e[0] for e in events]
        assert "DRAIN" in kinds
        assert kinds.index("DRAIN") < kinds.index("DELETE")

    def test_pages_collected_does_not_drain(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events,
                       get_results=[_Resp(200, b"p1"), _Resp(404)])
        _install_drain_recorder(monkeypatch, events)
        pages = escl_engine.execute_multipage_scan(
            _scanner(), timeout=5.0, page_timeout=5.0)
        assert len(pages) == 1
        kinds = [e[0] for e in events]
        assert "DRAIN" not in kinds


class TestPendingCleanupDrains:
    def test_pending_cleanup_drains_before_delete(self, monkeypatch):
        events = []
        escl_engine.remember_pending_job("http://10.0.0.9/eSCL/ScanJobs/j1")
        _install_drain_recorder(monkeypatch, events)
        sess = _FakeSession(events, delete_codes=[200])
        done = escl_engine.cleanup_pending_jobs(sess, "http://10.0.0.9/eSCL/")
        assert done == 1
        kinds = [e[0] for e in events]
        assert "DRAIN" in kinds, "补删时也必须先把文档取走"
        assert kinds.index("DRAIN") < kinds.index("DELETE")

    def test_drain_failure_does_not_block_delete(self, monkeypatch):
        events = []
        escl_engine.remember_pending_job("http://10.0.0.9/eSCL/ScanJobs/j1")

        def _boom(session, job_uri, max_gets=2, timeout=5.0):
            raise RuntimeError("取文档失败")

        monkeypatch.setattr(escl_engine, "_drain_job", _boom)
        sess = _FakeSession(events, delete_codes=[200])
        done = escl_engine.cleanup_pending_jobs(sess, "http://10.0.0.9/eSCL/")
        assert done == 1, "取文档失败也要照删"
        assert any(e[0] == "DELETE" for e in events)


class TestStaticGuards:
    """静态守住：别让人把收尾顺序改回去"""

    @pytest.mark.parametrize("func_name", [
        "execute_scan", "execute_multipage_scan", "cleanup_pending_jobs"])
    def test_cleanup_path_calls_drain(self, func_name):
        import inspect
        src = inspect.getsource(getattr(escl_engine, func_name))
        assert "_drain_job" in src, \
            "%s 的收尾路径必须调用 _drain_job（先取文档再删作业）" % func_name

    def test_drain_helper_exists(self):
        assert callable(escl_engine._drain_job)
