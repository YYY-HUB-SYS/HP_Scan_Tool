"""收尾契约：**只取文档，绝不 DELETE** — HP Scan Tool v4.0

★ 2026-10-24 真机实测坐实（见台账 §13.32，证据在 `.101` 与三台 M232 上）：

  ① **DELETE 的语义是「取消设备上的作业」** ——
     设备会把作业记成 `JobStateReason=JobCanceledByUser`
     （`.203` / `.208` / `.111` 的残留记录实测就是它；
       `.101` 正常收尾的是 `JobCompletedSuccessfully`）。
     ⇒ 在 M232 机型上，被取消的作业会让**扫描子系统永久锁死**：
       `POST /ScanJobs` 一律 **409**，而客户端再去 DELETE 只得到 **404/410**
       ⇒ **清不掉，只有断电重启能清**。
     ⇒ 这就是「三台打印机扫不了东西」的真正成因。

  ② **DELETE 本来就是多余的** ——
     文档一取走，设备**自己**就把作业记成 `Completed` 并回收；
     我们随后发出的 DELETE 拿到的是 **404**（作业早没了）。

★ 所以收尾的正解只有两步：
  - 把文档取走（`NextDocument`）；
  - 取不到「确定答复」（503 / 超时 / 异常）⇒ **不许 DELETE**，
    记进待清清单，**下一轮建 job 前再取**。

★ 本文件守的是**这条红线**：
  - 收尾三条路径（`execute_scan` / `execute_multipage_scan` /
    `cleanup_pending_jobs`）**一律不得发出 DELETE**；
  - 取不到确定答复 ⇒ 条目必须留在待清清单里；
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
    def test_no_document_drains_and_never_deletes(self, monkeypatch):
        """★ 一页都没拿到 ⇒ 先补取；**收尾绝不 DELETE**。"""
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(404), _Resp(404)])
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_scan(_scanner(), timeout=0.01)
        kinds = _kinds(events)
        assert "GET" in kinds, "收尾必须先把文档取走"
        assert "DELETE" not in kinds, \
            "★ 收尾发 DELETE = 取消设备上的作业 ⇒ 设备记 JobCanceledByUser " \
            "⇒ M232 永久锁死（409，客户端也清不掉）"

    def test_incomplete_drain_must_not_delete(self, monkeypatch):
        """★ 核心守护：设备还在扫描（503）⇒ **不许删作业**，要记进待清清单。"""
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(503)] * 4)
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_scan(_scanner(), timeout=0.01)
        assert "DELETE" not in _kinds(events), \
            "没拿到确定答复就删作业 ⇒ 设备随后攥住那一页 ⇒ 那台打印机会卡死"
        assert len(escl_engine.load_pending_jobs()) == 1, \
            "没收尾的作业必须记进待清清单，下一轮再取"

    def test_success_path_does_not_drain_again(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(200, b"jpgdata")])
        data, ext = escl_engine.execute_scan(_scanner(), timeout=5.0)
        assert data == b"jpgdata"
        kinds = _kinds(events)
        assert "DELETE" not in kinds, \
            "★ 文档一取走设备自己就记 Completed 并回收（DELETE 只是 404 空操作）；" \
            "发 DELETE 反而会 cancel 掉作业 ⇒ 锁死设备"
        assert not any(e[0] == "GET" and e[2] == CLEANUP_TIMEOUT for e in events), \
            "已经拿到过文档 ⇒ 不必再取（再取只会 404）"


class TestMultipageCleanup:
    def test_no_pages_drains_and_never_deletes(self, monkeypatch):
        events = []
        _install_fakes(monkeypatch, events, get_results=[_Resp(404)])
        with pytest.raises(escl_engine.ScanDeviceError):
            escl_engine.execute_multipage_scan(_scanner(), timeout=0.01, page_timeout=0.01)
        kinds = _kinds(events)
        assert "GET" in kinds, "收尾必须先把文档取走"
        assert "DELETE" not in kinds, \
            "★ 多页路径的收尾同样绝不能 DELETE（取消 = 锁死设备）"

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
    def test_pending_cleanup_drains_and_never_deletes(self, monkeypatch):
        """★ 补收尾 = 只取文档；**不发 DELETE**。"""
        events = []
        escl_engine.remember_pending_job("http://10.0.0.9/eSCL/ScanJobs/j1")
        sess = _FakeSession(events, get_results=[_Resp(404)], delete_codes=[200])
        done = escl_engine.cleanup_pending_jobs(sess, "http://10.0.0.9/eSCL/")
        assert done == 1
        kinds = _kinds(events)
        assert "GET" in kinds, "补收尾时必须先把文档取走"
        assert "DELETE" not in kinds, \
            "★ 补收尾也绝不能 DELETE（取消 = 锁死设备）"
        assert escl_engine.load_pending_jobs() == [], "处理完的条目必须从清单移除"

    def test_incomplete_drain_keeps_entry_and_does_not_delete(self, monkeypatch):
        """★ 设备还没答复 ⇒ 这一轮不处理、条目保留 ⇒ 下一轮继续取。"""
        events = []
        escl_engine.remember_pending_job("http://10.0.0.9/eSCL/ScanJobs/j1")
        sess = _FakeSession(events, get_results=[_Resp(503), _Resp(503)])
        done = escl_engine.cleanup_pending_jobs(sess, "http://10.0.0.9/eSCL/")
        assert done == 0
        assert "DELETE" not in _kinds(events), "没拿到确定答复就不许删"
        assert len(escl_engine.load_pending_jobs()) == 1, "条目必须保留，下轮再取"

    def test_drain_failure_keeps_entry_and_never_deletes(self, monkeypatch):
        """补取彻底失败（抛异常）⇒ 保留条目、下一轮再取；**绝不改用 DELETE**。"""
        events = []
        escl_engine.remember_pending_job("http://10.0.0.9/eSCL/ScanJobs/j1")

        def _boom(session, job_uri, *a, **kw):
            raise RuntimeError("取文档失败")

        monkeypatch.setattr(escl_engine, "_drain_job", _boom)
        sess = _FakeSession(events, delete_codes=[200])
        done = escl_engine.cleanup_pending_jobs(sess, "http://10.0.0.9/eSCL/")
        assert done == 0, "取文档彻底失败 ⇒ 不知道设备状态 ⇒ 不能算处理完"
        assert "DELETE" not in _kinds(events), \
            "★ 兜底路径更不能 DELETE（那正是当初把机器搞废的动作）"
        assert len(escl_engine.load_pending_jobs()) == 1, "条目必须保留，下轮再取"


class TestStaticGuards:
    """静态守住：别让人把收尾改回「取完就 DELETE」"""

    @pytest.mark.parametrize("func_name", [
        "execute_scan", "execute_multipage_scan", "cleanup_pending_jobs"])
    def test_cleanup_path_calls_drain(self, func_name):
        import inspect
        src = inspect.getsource(getattr(escl_engine, func_name))
        assert "_drain_job" in src, \
            "%s 的收尾路径必须调用 _drain_job（先把文档取走）" % func_name

    @pytest.mark.parametrize("func_name", [
        "execute_scan", "execute_multipage_scan", "cleanup_pending_jobs"])
    def test_cleanup_path_never_deletes(self, func_name):
        """★★ 红线：收尾**绝不能**发 DELETE。

        DELETE = 「取消设备上的作业」⇒ 设备记 `JobCanceledByUser`
        ⇒ M232 机型上扫描子系统被永久锁死（POST 一律 409），
        客户端再 DELETE 只得 404/410 ⇒ 只有断电重启能清。
        """
        import inspect
        src = inspect.getsource(getattr(escl_engine, func_name))
        assert ".delete(" not in src, \
            "★ %s 的收尾里出现了 DELETE ⇒ 会把设备上的作业 cancel 掉 ⇒ " \
            "M232 永久锁死（见台账 §13.32）" % func_name

    def test_drain_helper_exists(self):
        assert callable(escl_engine._drain_job)
