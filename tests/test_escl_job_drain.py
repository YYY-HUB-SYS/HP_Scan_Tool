"""收尾契约：**先把文档取走，再删作业** — HP Scan Tool v4.0

★ 为什么必须有这一步（2026-10-23 真机实测，见台账 §13.23）：
  设备在 POST 之后会**自己扫好一页、把它攥在手里**
  （ScannerStatus 里表现为 ``Processing`` + ``ImagesCompleted=1``）。
  客户端如果**不取走文档就直接 DELETE** ⇒ 设备端的「忙」标志**解不开**：
  实测 DELETE 返回 **200**，设备**仍然 409** 拒接新作业
  ⇒ 那台打印机在很长一段时间里扫不了东西。

★ 2026-10-24 追加的第二条（更隐蔽）：
  **取不到「确定答复」时，绝不能删作业。**
  设备还在扫描时会回 **503**（= 还没准备好）—— 这时如果照常 DELETE：
  设备随后扫出那一页、却已经没人能取走（事后 NextDocument ⇒ **410**）
  ⇒ 那台打印机就永久卡在「拒接新作业」上。
  ⇒ 正确处置：**这一轮不删**，把它记进待清清单，**下一轮建 job 前再取**。

★ 本文件守的是**顺序与边界**：
  - 失败/取消/无文档（没拿到过文档）⇒ **必须补取**再删；
  - 补取拿到**确定答复**（200 / 404 / 410）⇒ 可以删；
  - 补取**没拿到确定答复**（503 / 超时 / 异常）⇒ **不许删**，要记进清单；
  - 已经拿到过文档（成功路径）⇒ **不必**再取（再取只会 404，白花时间）。
"""

import os

import pytest

import escl_engine

# 收尾补取用的超时值（与取页轮询的 15 秒故意不同，便于测试区分）
CLEANUP_TIMEOUT = 3.0


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
        self.events.append(("GET", url, timeout))
        if self.get_results:
            return self.get_results.pop(0)
        return _Resp(404)

    def delete(self, url, timeout=None, **kw):
        self.events.append(("DELETE", url, timeout))
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


def _kinds(events):
    return [e[0] for e in events]


class TestDrainHelper:
    def test_returns_bytes_and_complete(self):
        sess = _FakeSession([], get_results=[_Resp(200, b"abc"), _Resp(404)])
        total, complete = escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j")
        assert total == 3
        assert complete is True
        assert len(sess.events) == 2, "应当继续取下一页"

    def test_404_is_a_definite_answer(self):
        sess = _FakeSession([], get_results=[_Resp(404)])
        _total, complete = escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j")
        assert complete is True

    def test_410_is_a_definite_answer(self):
        sess = _FakeSession([], get_results=[_Resp(410)])
        _total, complete = escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j")
        assert complete is True

    def test_503_is_not_definite(self):
        """★ 设备还在扫描 ⇒ 没有确定答复 ⇒ 调用方**不许删作业**。"""
        sess = _FakeSession([], get_results=[_Resp(503), _Resp(503), _Resp(503)])
        _total, complete = escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j",
                                                   max_gets=3, busy_wait=0)
        assert complete is False
        assert len(sess.events) == 3, "503 应当重试到用完次数"

    def test_exception_is_not_definite(self):
        class _Boom:
            def get(self, *a, **k):
                raise RuntimeError("设备不回应")

        _total, complete = escl_engine._drain_job(_Boom(), "http://h/eSCL/ScanJobs/j")
        assert complete is False

    def test_empty_uri_is_noop(self):
        sess = _FakeSession([])
        assert escl_engine._drain_job(sess, "") == (0, True)
        assert sess.events == []

    def test_uses_nextdocument_url(self):
        sess = _FakeSession([], get_results=[_Resp(404)])
        escl_engine._drain_job(sess, "http://h/eSCL/ScanJobs/j/")
        assert sess.events[0][1] == "http://h/eSCL/ScanJobs/j/NextDocument"


class TestExecuteScanCleanup:
    def test_no_document_drains_before_delete(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(404), _Resp(404)])
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_scan(_scanner(), timeout=0.01)
        kinds = _kinds(events)
        assert "DELETE" in kinds
        assert kinds.index("GET") < kinds.index("DELETE"), \
            "顺序错：必须先取文档再删作业"

    def test_incomplete_drain_must_not_delete(self, monkeypatch):
        """★ 核心守护：设备还在扫描（503）⇒ **不许删作业**，要记进待清清单。"""
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(503)] * 4)
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_scan(_scanner(), timeout=0.01)
        assert "DELETE" not in _kinds(events), \
            "没拿到确定答复就删作业 ⇒ 设备随后攥住那一页 ⇒ 那台打印机会卡死"
        assert len(escl_engine.load_pending_jobs()) == 1, \
            "没删掉的作业必须记进待清清单，下一轮再取"

    def test_success_path_does_not_drain_again(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(200, b"jpgdata")])
        data, ext = escl_engine.execute_scan(_scanner(), timeout=5.0)
        assert data == b"jpgdata"
        kinds = _kinds(events)
        assert "DELETE" in kinds, "收尾仍然必须删作业"
        assert not any(e[0] == "GET" and e[2] == CLEANUP_TIMEOUT for e in events), \
            "已经拿到过文档 ⇒ 不必再取（再取只会 404）"


class TestMultipageCleanup:
    def test_no_pages_drains_then_deletes(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(404)])
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_multipage_scan(_scanner(), timeout=0.01, page_timeout=0.01)
        kinds = _kinds(events)
        assert kinds.index("GET") < kinds.index("DELETE")

    def test_incomplete_drain_must_not_delete(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(503)] * 4)
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_multipage_scan(_scanner(), timeout=0.01, page_timeout=0.01)
        assert "DELETE" not in _kinds(events), \
            "没拿到确定答复就删作业 ⇒ 设备随后攥住那一页 ⇒ 那台打印机会卡死"
        assert len(escl_engine.load_pending_jobs()) == 1

    def test_pages_collected_does_not_drain(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events,
                       get_results=[_Resp(200, b"p1"), _Resp(404)])
        pages = escl_engine.execute_multipage_scan(
            _scanner(), timeout=5.0, page_timeout=5.0)
        assert len(pages) == 1
        assert not any(e[0] == "GET" and e[2] == CLEANUP_TIMEOUT for e in events)


class TestPendingCleanupDrains:
    def test_pending_cleanup_drains_before_delete(self, monkeypatch):
        events = []
        escl_engine.remember_pending_job("http://10.0.0.9/eSCL/ScanJobs/j1")
        sess = _FakeSession(events, get_results=[_Resp(404)], delete_codes=[200])
        done = escl_engine.cleanup_pending_jobs(sess, "http://10.0.0.9/eSCL/")
        assert done == 1
        kinds = _kinds(events)
        assert kinds.index("GET") < kinds.index("DELETE"), \
            "补删时也必须先把文档取走"

    def test_incomplete_drain_keeps_entry_and_does_not_delete(self, monkeypatch):
        """★ 设备还没答复 ⇒ 这一轮不删、条目保留 ⇒ 下一轮继续取。"""
        events = []
        escl_engine.remember_pending_job("http://10.0.0.9/eSCL/ScanJobs/j1")
        sess = _FakeSession(events, get_results=[_Resp(503), _Resp(503)])
        done = escl_engine.cleanup_pending_jobs(sess, "http://10.0.0.9/eSCL/")
        assert done == 0
        assert "DELETE" not in _kinds(events), "没拿到确定答复就不许删"
        assert len(escl_engine.load_pending_jobs()) == 1, "条目必须保留，下轮再取"

    def test_drain_failure_does_not_block_delete(self, monkeypatch):
        """补取彻底失败（抛异常）⇒ 仍然要删（否则清单永远清不掉）。"""
        events = []
        escl_engine.remember_pending_job("http://10.0.0.9/eSCL/ScanJobs/j1")

        def _boom(session, job_uri, *a, **kw):
            raise RuntimeError("取文档失败")

        monkeypatch.setattr(escl_engine, "_drain_job", _boom)
        sess = _FakeSession(events, delete_codes=[200])
        done = escl_engine.cleanup_pending_jobs(sess, "http://10.0.0.9/eSCL/")
        assert done == 1, "取文档彻底失败也要照删"
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
