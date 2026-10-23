"""
eSCL 多页扫描整链路 + 取页轮询取消语义测试 — HP Scan Tool v4.0

覆盖矩阵里的两条缺口，都是 `escl_engine.py`（协议层）的**核心链路**：

  链号 1.14 `execute_multipage_scan` **整链路**
      此前**全库零行为级用例**（只有 `tests/test_escl_dead_code.py` 里的
      `assert callable`）。本文件补上完整序列：
      建 job（POST /ScanJobs）→ 轮询取页（GET NextDocument）→ 收尾（404/410）
      → 清理 job（DELETE），并锁死页数 / 页序 / 页字节 / 扩展名 /
      进度回调次数与参数 / DELETE 真的发生 / 取消语义。

  链号 1.15 取页轮询期间取消 ⇒ **返回空结果**（不抛异常）
      `execute_scan` 轮询体里的 `cancel_event.is_set()` 分支此前零断言。

★ 断言全部按 `escl_engine.py` 的**实际实现**写，不按覆盖矩阵的描述臆测：
  - 取页轮询打的是 `GET {job_uri}/NextDocument`（**不是** `GET /ScanJobs/<id>`）——
    实现里没有任何轮询 JobState 的请求；404 即"没有更多页"的结束信号；
  - `execute_scan` 取消 ⇒ 返回 `(None, ext)`（**不是**空 bytes、**不是**抛异常）；
  - `execute_multipage_scan` 轮询中取消 ⇒ 返回**已取到的部分页**（list）；
    一页都没取到 ⇒ 返回空 list；
  - `execute_multipage_scan` 在**建 job 阶段**被取消 ⇒ 返回 `([], ext)`
    —— 注意这是 **2 元组**而不是 list，与上面两个取消分支**形态不一致**
    （产品代码现状；本文件只如实断言，**不改产品代码**，已上报主控拍板）。

★ **2026-10-23 增补**：取消/失败的收尾现在会**补取一次文档**再删作业
  （`escl_engine._drain_job`，**3 秒超时、有界**）——
  真机实测（台账 §13.23）：设备 POST 之后会自己扫好一页**攥在手里**，
  客户端不取走就 DELETE ⇒ 设备的「忙」标志解不开 ⇒ **那台打印机从此 409**。
  ⇒ 守护口径相应调整为：
    · **轮询取页**（超时 15 秒）：取消后必须**立刻停止**（计数必须保持原样）；
    · **收尾补取**（超时 3 秒）：**至多允许 1 次** —— 它不是"继续轮询"。

★ 本文件**不发任何真实 HTTP**：`requests.Session` 被替换成记录用的假 session，
另有 autouse 护栏把 `requests.adapters.HTTPAdapter.send` 打成必炸 —— 万一实现
绕过假 session 直接调 requests，会立刻失败而不是偷偷打真机。
"""

import threading
import time as _time_mod
from unittest import mock

import pytest

import escl_engine
from escl_engine import ScannerInfo


# ---- 固定装置：全部指向假设备 1.2.3.4，绝不触碰真机 ----
ESCL_BASE = "http://1.2.3.4:80/eSCL"
SCANJOBS_URL = ESCL_BASE + "/ScanJobs"
JOB_URI = ESCL_BASE + "/ScanJobs/42"
ND_URL = JOB_URI + "/NextDocument"

PAGE1 = b"\xff\xd8PAGE-1\xff\xd9"
PAGE2 = b"\xff\xd8PAGE-2\xff\xd9"
PAGE3 = b"\xff\xd8PAGE-3\xff\xd9"

# ★ 收尾补取（`_drain_job`）用的超时值 —— 与取页轮询的 15 秒**故意不同**，
#   好让守护测试能区分"还在轮询"与"收尾时把设备攥着的那页取走"。
_CLEANUP_TIMEOUT = 3.0


class _Resp:
    """最小 response 替身 —— 只提供协议层真正读到的字段。"""

    def __init__(self, status_code, content=b"", text="", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = headers or {}


class _FakeSession:
    """记录全部 HTTP 调用的假 session —— 一个真实请求都不发。

    `get_resps` 里的元素可以是：
      - `_Resp`        → 直接返回；
      - 可调用对象     → 以 `(session, url)` 调用它现场造响应
                        （用来在取页途中触发取消）。
    """

    def __init__(self, post_resp=None, get_resps=()):
        self._post_resp = post_resp
        self._get_resps = list(get_resps)
        self.calls = []            # [(method, url), ...] 按真实顺序
        self.post_body = None
        self.post_headers = None
        self.post_timeout = None
        self.get_timeouts = []
        self.deleted = []
        self.closed = False
        self.verify = True

    # ---------- requests.Session 的最小实现 ----------
    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url))
        self.post_body = data
        self.post_headers = headers
        self.post_timeout = timeout
        return self._post_resp

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        self.get_timeouts.append(timeout)
        if not self._get_resps:
            if timeout == _CLEANUP_TIMEOUT:
                # ★ 收尾补取是**有界**的：队列空了 ⇒ 「没有更多文档」⇒ 404
                #   （确定答复 ⇒ 调用方可以放心删作业）。
                #   注意：**取页轮询**（timeout=15）队列空了仍然要炸 ——
                #   那条守护（「轮询不许多发」）保持原样，不放宽。
                return _Resp(404)
            raise AssertionError("协议层发出了多余的 GET 请求: %s" % url)
        nxt = self._get_resps.pop(0)
        if callable(nxt):
            return nxt(self, url)
        return nxt

    def delete(self, url, timeout=None):
        self.calls.append(("DELETE", url))
        self.deleted.append(url)
        return _Resp(200)

    def close(self):
        self.closed = True

    # ---------- 断言辅助 ----------
    def methods(self):
        return [m for m, _u in self.calls]

    def urls(self, method):
        return [u for m, u in self.calls if m == method]

    def polling_gets(self):
        """**轮询取页**的 GET（取页超时 15 秒）。

        ★ 收尾补取（`_drain_job`）用的是 **3 秒**超时 ⇒ 用超时值就能把
          「还在轮询」与「收尾时把设备攥着的那页取走」区分开 ——
          守护要盯的是**别继续轮询**，不是「一次 HTTP 都不许发」。
        """
        return [t for t in self.get_timeouts if t != _CLEANUP_TIMEOUT]

    def cleanup_gets(self):
        """**收尾补取**的 GET（有界：每次 3 秒超时、至多 3 次）。"""
        return [t for t in self.get_timeouts if t == _CLEANUP_TIMEOUT]

    @property
    def pending_gets(self):
        """没被消费掉的 GET 响应数 —— 用来证明"取消后立刻停止轮询"。"""
        return len(self._get_resps)


class _FakeClock:
    """`time` 模块替身：sleep 不真睡（用例不拖时间），time 走真表。"""

    def __init__(self):
        self.slept = []

    def time(self):
        return _time_mod.time()

    def sleep(self, seconds):
        self.slept.append(seconds)


def _scanner():
    s = ScannerInfo(name="fake-adf", ip="1.2.3.4")
    s.escl_url = ESCL_BASE + "/"
    s.has_adf = True
    return s


def _run_multipage(session, **kwargs):
    """在假 session 上跑 `execute_multipage_scan`（网络层全部替换）。"""
    clock = _FakeClock()
    with mock.patch.object(escl_engine, "time", clock), \
            mock.patch.object(escl_engine.requests, "Session",
                              return_value=session):
        return escl_engine.execute_multipage_scan(scanner=_scanner(), **kwargs)


def _run_scan(session, **kwargs):
    """在假 session 上跑 `execute_scan`（网络层全部替换）。"""
    clock = _FakeClock()
    with mock.patch.object(escl_engine, "time", clock), \
            mock.patch.object(escl_engine.requests, "Session",
                              return_value=session):
        return escl_engine.execute_scan(scanner=_scanner(), **kwargs)


def _session(get_resps=(), post_status=201, post_headers=None):
    """造一个假 session：POST 建 job 返回 201 + Location，GET 按队列应答。"""
    headers = {"Location": JOB_URI} if post_headers is None else post_headers
    return _FakeSession(post_resp=_Resp(post_status, headers=headers),
                        get_resps=list(get_resps))


@pytest.fixture(autouse=True)
def _no_real_http():
    """护栏：本文件的用例**一个真实请求都不许发**。

    假 session 之外的任何真实 HTTP 尝试都会在这里立刻炸掉，
    而不是悄悄打到真机上。
    """
    with mock.patch("requests.adapters.HTTPAdapter.send",
                    side_effect=AssertionError("测试里不允许发真实 HTTP 请求")):
        yield


# ==================================================================
# 链号 1.14 —— execute_multipage_scan 整链路
# ==================================================================

class TestMultipageFullChain:
    """建 job → 轮询取页 → 收尾 → 清理 job 的完整序列。"""

    def test_http_sequence_is_create_poll_then_delete(self):
        """动作序列必须是 POST → GET(×N) → DELETE，且 URL 逐个对得上。"""
        session = _session([_Resp(200, content=PAGE1),
                            _Resp(200, content=PAGE2),
                            _Resp(404)])

        _run_multipage(session, source="Feeder")

        assert session.methods() == ["POST", "GET", "GET", "GET", "DELETE"], \
            "动作序列不对: %r" % (session.calls,)
        assert session.urls("POST") == [SCANJOBS_URL], \
            "建 job 必须打到 /ScanJobs，实际: %r" % (session.urls("POST"),)
        assert session.urls("GET") == [ND_URL, ND_URL, ND_URL], \
            "取页必须打到 {job_uri}/NextDocument，实际: %r" % (session.urls("GET"),)
        assert session.deleted == [JOB_URI], \
            "收尾必须 DELETE 建出来的 job，实际: %r" % (session.deleted,)

    def test_page_count_order_and_bytes(self):
        """页数 / 页序 / 每页字节内容 —— 少取一页或顺序颠倒都必须红。"""
        session = _session([_Resp(200, content=PAGE1),
                            _Resp(200, content=PAGE2),
                            _Resp(200, content=PAGE3),
                            _Resp(404)])

        result = _run_multipage(session, source="Feeder")

        assert isinstance(result, list), "返回的必须是列表，实际: %r" % (type(result),)
        assert len(result) == 3, "页数不对: %r" % (len(result),)
        assert [data for data, _ext in result] == [PAGE1, PAGE2, PAGE3], \
            "页序或页字节内容不对: %r" % ([data for data, _e in result],)

    def test_each_page_carries_the_requested_extension(self):
        """每一页都要带上落盘扩展名（协议层按 REQUEST_FORMAT 定）。"""
        session = _session([_Resp(200, content=PAGE1),
                            _Resp(200, content=PAGE2),
                            _Resp(404)])

        result = _run_multipage(session, source="Feeder", output_format="jpg")

        assert [ext for _data, ext in result] == ["jpg", "jpg"]
        assert all(isinstance(ext, str) and ext for _data, ext in result)

    def test_progress_callback_called_once_per_page(self):
        """进度回调：每取到一页调一次，参数是 (已取页数, 已取页数)。"""
        calls = []
        session = _session([_Resp(200, content=PAGE1),
                            _Resp(200, content=PAGE2),
                            _Resp(404)])

        _run_multipage(session, source="Feeder",
                       progress_callback=lambda *a: calls.append(a))

        assert len(calls) == 2, "回调次数应等于页数，实际 %d 次: %r" % (len(calls), calls)
        assert calls == [(1, 1), (2, 2)], "回调参数不对: %r" % (calls,)

    def test_progress_callback_is_optional(self):
        """不传 progress_callback 也要把整条链路跑完（回调是可选的）。"""
        session = _session([_Resp(200, content=PAGE1),
                            _Resp(200, content=PAGE2),
                            _Resp(404)])

        result = _run_multipage(session, source="Feeder", progress_callback=None)

        assert len(result) == 2
        assert [data for data, _e in result] == [PAGE1, PAGE2]

    def test_job_is_deleted_and_session_closed(self):
        """收尾必须 DELETE job（防止设备侧任务累积 → 后续 409）并关闭 session。"""
        session = _session([_Resp(200, content=PAGE1), _Resp(404)])

        _run_multipage(session, source="Feeder")

        assert session.deleted == [JOB_URI]
        assert session.calls[-1] == ("DELETE", JOB_URI), \
            "DELETE 必须是最后一个动作，实际: %r" % (session.calls,)
        assert session.closed is True, "session 必须关闭"

    def test_adf_request_carries_multipage_contract(self):
        """ADF（Feeder）建 job 的请求体必须带 MaxScanPages=1000 与正确的来源。"""
        session = _session([_Resp(200, content=PAGE1), _Resp(404)])

        _run_multipage(session, source="Feeder")

        assert session.post_headers == {"Content-Type": "application/xml"}
        assert session.post_timeout == 30
        body = session.post_body
        assert "<scan:MaxScanPages>1000</scan:MaxScanPages>" in body, \
            "ADF 多页必须请求 MaxScanPages=1000，实际请求体: %s" % body
        assert "<pwg:InputSource>Feeder</pwg:InputSource>" in body
        assert "<pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>" in body

    def test_platen_request_has_no_max_scan_pages(self):
        """平板（Platen）不得带 MaxScanPages —— 多页是 ADF 专属。"""
        session = _session([_Resp(200, content=PAGE1), _Resp(404)])

        _run_multipage(session, source="Platen")

        assert "MaxScanPages" not in session.post_body, \
            "平板请求体不该有 MaxScanPages: %s" % session.post_body

    def test_404_ends_scan_without_one_extra_poll(self):
        """404 是"没有更多页"的结束信号 —— 取完就必须停，不得再多打一次。"""
        session = _session([_Resp(200, content=PAGE1),
                            _Resp(200, content=PAGE2),
                            _Resp(404)])

        result = _run_multipage(session, source="Feeder")

        assert len(result) == 2
        assert len(session.urls("GET")) == 3, \
            "GET 次数不对: %r" % (session.urls("GET"),)
        assert session.pending_gets == 0, "不该有没消费完的取页响应"

    def test_410_keeps_pages_already_fetched(self):
        """410（扫描过期）⇒ 保留已取到的页，不抛异常。"""
        session = _session([_Resp(200, content=PAGE1),
                            _Resp(410)])

        result = _run_multipage(session, source="Feeder")

        assert result == [(PAGE1, "jpg")], "410 时应保留已取页，实际: %r" % (result,)
        assert session.deleted == [JOB_URI]


class TestMultipageCancel:
    """`execute_multipage_scan` 的取消语义（按实际实现断言）。"""

    def test_cancel_during_polling_returns_partial_pages(self):
        """取页途中取消 ⇒ 返回**已取到的部分页**，不抛异常。"""
        cancel = threading.Event()

        def _cancel_now(_session, _url):
            cancel.set()
            return _Resp(503)

        session = _session([_Resp(200, content=PAGE1), _cancel_now])
        calls = []

        result = _run_multipage(session, source="Feeder", cancel_event=cancel,
                                progress_callback=lambda *a: calls.append(a))

        assert result == [(PAGE1, "jpg")], \
            "取消时应返回已取到的 1 页，实际: %r" % (result,)
        assert calls == [(1, 1)], "回调只应触发一次（第一页），实际: %r" % (calls,)
        assert len(session.urls("GET")) == 2, "取消后应立刻停止轮询"

    def test_cancel_before_first_page_returns_empty_list(self):
        """一开始就已取消 ⇒ 返回空列表，且一次取页请求都不该发。"""
        cancel = threading.Event()
        cancel.set()
        session = _session([_Resp(200, content=PAGE1)])

        result = _run_multipage(session, source="Feeder", cancel_event=cancel)

        assert isinstance(result, list), "返回的必须是列表，实际: %r" % (type(result),)
        assert result == [], "取消且未取到页时应返回空列表，实际: %r" % (result,)
        assert session.polling_gets() == [], \
            "已取消就不该再轮询取页，实际轮询 %d 次" % len(session.polling_gets())
        # ★ 允许**有界（至多 3 次）**的收尾补取（每次 3 秒超时）：设备可能还攥着那一页，
        #   不取走它 ⇒ 那台打印机会从此拒接新作业（409）。
        assert len(session.cleanup_gets()) <= 3, \
            "收尾补取有界（至多 3 次），实际 %d 次" % len(session.cleanup_gets())

    def test_cancel_still_deletes_job(self):
        """取消也要清理设备上的 job（否则设备侧任务累积 → 后续 409）。"""
        cancel = threading.Event()
        cancel.set()
        session = _session([_Resp(200, content=PAGE1)])

        _run_multipage(session, source="Feeder", cancel_event=cancel)

        assert session.deleted == [JOB_URI], \
            "取消后也必须 DELETE job，实际: %r" % (session.deleted,)

    def test_cancel_during_job_creation_returns_empty_pages_tuple(self):
        """★ 建 job 阶段（409 退避）被取消 ⇒ 返回 `([], ext)`。

        注意这里的形态与上面两个取消分支**不一致**：返回的是 **2 元组**
        `([], "jpg")`，不是列表。这是产品代码的现状，本文件如实断言，
        不改产品代码（已上报主控拍板是否统一形态）。
        """
        cancel = threading.Event()
        cancel.set()
        session = _session(post_status=409, post_headers={})

        result = _run_multipage(session, source="Feeder", cancel_event=cancel)

        assert result == ([], "jpg"), \
            "建 job 阶段取消的返回形态是 ([], ext)，实际: %r" % (result,)
        assert len(session.urls("POST")) == 1, \
            "取消后不得再重试建 job，实际 POST %d 次" % len(session.urls("POST"))


# ==================================================================
# 链号 1.15 —— execute_scan 取页轮询期间取消 ⇒ 返回空结果
# ==================================================================

class TestExecuteScanCancelReturnsEmpty:
    """取消 ⇒ 返回 `(None, ext)`；**不抛异常**（尤其不是 ScanCancelled）。"""

    def test_cancel_before_polling_returns_none_data(self):
        """进入轮询前就已取消 ⇒ 返回 (None, ext)，且一次取页都不发。"""
        cancel = threading.Event()
        cancel.set()
        session = _session([_Resp(200, content=PAGE1)])

        result = _run_scan(session, cancel_event=cancel)

        assert isinstance(result, tuple), "返回的必须是元组，实际: %r" % (type(result),)
        assert len(result) == 2, "返回的必须是 (数据, 扩展名)，实际: %r" % (result,)
        assert result[0] is None, "取消时数据必须是 None（不是空 bytes），实际: %r" % (result[0],)
        assert result[1] == "jpg", "取消时仍要报出扩展名，实际: %r" % (result[1],)
        assert session.polling_gets() == [], \
            "已取消就不该轮询取页，实际轮询 %d 次" % len(session.polling_gets())
        assert len(session.cleanup_gets()) <= 3, \
            "收尾补取有界（至多 3 次），实际 %d 次" % len(session.cleanup_gets())

    def test_cancel_during_polling_returns_none_data(self):
        """取页轮询途中被取消 ⇒ 返回 (None, ext)，并立刻停止轮询。"""
        cancel = threading.Event()

        def _cancel_now(_session, _url):
            cancel.set()
            return _Resp(503)          # 扫描进行中；下一轮检查就会看到取消

        session = _session([_cancel_now, _Resp(200, content=PAGE1)])

        result = _run_scan(session, cancel_event=cancel)

        assert result == (None, "jpg"), \
            "轮询中取消必须返回 (None, ext)，实际: %r" % (result,)
        assert len(session.polling_gets()) == 1, \
            "取消后必须立刻停止轮询，实际轮询 GET %d 次" % len(session.polling_gets())
        # ★ 收尾补取（有界：每次 3 秒超时、至多 3 次）允许发生 —— 它不是"继续轮询"，
        #   而是去把设备还攥着的那一页取走，否则那台打印机会卡成 409。
        assert len(session.cleanup_gets()) <= 3, \
            "收尾补取有界（至多 3 次），实际 %d 次" % len(session.cleanup_gets())

    def test_cancel_does_not_raise_any_exception(self):
        """★ 取消**不抛异常** —— 既不是 ScanCancelled，也不是别的类型。"""
        cancel = threading.Event()
        cancel.set()
        session = _session([_Resp(200, content=PAGE1)])
        clock = _FakeClock()

        with mock.patch.object(escl_engine, "time", clock), \
                mock.patch.object(escl_engine.requests, "Session",
                                  return_value=session):
            try:
                result = escl_engine.execute_scan(scanner=_scanner(),
                                                  cancel_event=cancel)
            except escl_engine.ScanCancelled as exc:
                pytest.fail("取页轮询期间取消必须返回空结果，不得抛 ScanCancelled: %r" % (exc,))
            except Exception as exc:
                pytest.fail("取页轮询期间取消必须返回空结果，不得抛其它异常: %r" % (exc,))

        assert result == (None, "jpg"), "取消的返回形态必须是 (None, ext)，实际: %r" % (result,)

    def test_cancel_still_deletes_job(self):
        """取消也要清理设备上的 job。"""
        cancel = threading.Event()
        cancel.set()
        session = _session([_Resp(200, content=PAGE1)])

        _run_scan(session, cancel_event=cancel)

        assert session.deleted == [JOB_URI], \
            "取消后也必须 DELETE job，实际: %r" % (session.deleted,)
