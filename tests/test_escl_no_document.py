"""
「设备 0 页」**不得**被报成「用户取消」— 契约测试 — HP Scan Tool v4.0

★ 背景（用户 2026-10-22 在另一台机器上实测原话：「扫描会自动取消」）：
  ADF（输稿器）路径上，`execute_multipage_scan` 把「取页时**第一个**
  NextDocument 就回 404**」当成「没有更多页了」**立刻 return**（哪怕 0 页）
  ⇒ 返回空列表 ⇒ 协调器按**取消**处理 ⇒ 界面显示「扫描已取消」，
  而真实原因（输稿器没纸 / 设备里没东西可扫）**一个字都没告诉用户**。

★ eSCL 规范语义（node-hp-scan-to 的抓包文档逐字引用 Mopria eSCL 规范）：
    404 = "no more page. The last page has already been transmitted."
    410 = "the scan job doesn't exist anymore."
    503 = "the job is active, but the scanner can't return the payload at
           the moment. Retry later."
  ⇒「还没准备好」是 **503** 的语义；404/410 一页都没有 ⇒ 作业确实没产出文档。
  ⇒ 公网实现（NAPS2 有 DeviceFeederEmptyException、sane-airscan 有
    SANE_STATUS_NO_DOCS）都有独立的"无文档"状态，**没有一个**把 0 页报成取消。

★ 本文件锁死的新契约：
  ① **已经拿到过页**之后的 404/410 ⇒ 正常收尾（返回已拿到的页）—— 不变；
  ② **一页都没拿到**的 404/410 ⇒ 抛 `ScanDeviceError`（带可执行指引），
     **绝不**返回空列表（空列表 = 取消）；
  ③ 设备还在忙（Processing / 有未完成 job）⇒ **继续等**，不误报失败；
  ④ 文案按来源分流：输稿器 ⇒ 提示放纸；平板 ⇒ 提示放文档；
     AdfState 是卡纸/盖板/托盘类 ⇒ 提示处理卡纸。

★ 本文件**不发任何真实 HTTP**（`requests.Session` 被换成记录用的假 session）。
"""

import threading
import time as _time_mod
from unittest import mock

import pytest

import escl_engine
from escl_engine import ScannerInfo


ESCL_BASE = "http://1.2.3.4:80/eSCL/"
JOB_URI = "http://1.2.3.4:80/eSCL/ScanJobs/7"
ND_URL = JOB_URI + "/NextDocument"


class _Resp:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


class _FakeSession:
    """记录全部 HTTP 调用的假 session —— 一个真实请求都不发。"""

    def __init__(self, get_resps=()):
        self._get_resps = list(get_resps)
        self.calls = []
        self.deleted = []
        self.closed = False
        self.verify = True

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url))
        return _Resp(201, headers={"Location": JOB_URI})

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        if not self._get_resps:
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

    def urls(self, method):
        return [u for m, u in self.calls if m == method]


class _FakeClock:
    """`time` 替身：sleep 不真睡，time 走真表。"""

    def __init__(self):
        self.slept = []

    def time(self):
        return _time_mod.time()

    def sleep(self, seconds):
        self.slept.append(seconds)


def _scanner():
    s = ScannerInfo(name="fake-adf", ip="1.2.3.4")
    s.escl_url = ESCL_BASE
    s.has_adf = True
    return s


def _run_multipage(session, status_probe, **kwargs):
    """在假 session 上跑 execute_multipage_scan（网络层全替换）。

    `status_probe` 是 `probe_scanner_status` 的替身返回值 (ok, dict)。
    """
    clock = _FakeClock()
    with mock.patch.object(escl_engine, "time", clock), \
            mock.patch.object(escl_engine.requests, "Session",
                              return_value=session), \
            mock.patch.object(escl_engine, "probe_scanner_status",
                              return_value=status_probe):
        return escl_engine.execute_multipage_scan(scanner=_scanner(), **kwargs)


IDLE = (True, {"state": "Idle", "adf_loaded": False, "adf_state": "",
                "pending_jobs": 0, "job_states": []})
BUSY = (True, {"state": "Processing", "adf_loaded": True, "adf_state": "",
               "pending_jobs": 1, "job_states": ["Processing"]})


class TestNoDocumentIsNotCancel:
    """★ 核心：设备 0 页 ≠ 用户取消"""

    def test_first_404_with_no_pages_raises_readable_error(self):
        """★ 第一个 NextDocument 就 404（一页都没有）⇒ 抛可读错误，**不是**空列表"""
        session = _FakeSession([_Resp(404)] * 40)
        with pytest.raises(escl_engine.ScanDeviceError) as exc:
            _run_multipage(session, IDLE, source="Feeder")
        msg = str(exc.value)
        assert "没有返回任何扫描数据" in msg, msg
        assert "输稿器" in msg, "输稿器来源必须提示放纸: %s" % msg
        assert "纸" in msg, msg

    def test_platen_source_gets_its_own_message(self):
        """平板来源 ⇒ 提示放文档（不能把用户指到输稿器）"""
        session = _FakeSession([_Resp(404)] * 40)
        with pytest.raises(escl_engine.ScanDeviceError) as exc:
            _run_multipage(session, IDLE, source="Platen")
        msg = str(exc.value)
        assert "平板" in msg, msg
        assert "输稿器" not in msg, f"平板文案不得指向输稿器: {msg}"

    def test_jam_adf_state_gets_jam_guidance(self):
        """★ AdfState 是卡纸/盖板/托盘类 ⇒ 提示处理卡纸（不是"没纸"）"""
        session = _FakeSession([_Resp(404)] * 40)
        jam = (True, {"state": "Idle", "adf_loaded": False,
                       "adf_state": "ScannerAdfJam", "pending_jobs": 0,
                       "job_states": []})
        with pytest.raises(escl_engine.ScanDeviceError) as exc:
            _run_multipage(session, jam, source="Feeder")
        msg = str(exc.value)
        assert "卡纸" in msg, msg

    def test_404_after_pages_still_ends_normally(self):
        """★ 已经拿到过页之后的 404 ⇒ 正常收尾（**行为不变**）"""
        session = _FakeSession([_Resp(200, content=b"P1"), _Resp(404)])
        result = _run_multipage(session, IDLE, source="Feeder")
        assert [d for d, _e in result] == [b"P1"]
        assert session.deleted == [], \
            "★ 收尾绝不能 DELETE（取消 = 锁死设备），实际: %r" % (session.deleted,)

    def test_410_after_pages_still_ends_normally(self):
        """已经拿到过页之后的 410 ⇒ 正常收尾（保留已拿到的页）"""
        session = _FakeSession([_Resp(200, content=b"P1"), _Resp(410)])
        result = _run_multipage(session, IDLE, source="Feeder")
        assert [d for d, _e in result] == [b"P1"]

    def test_device_busy_keeps_waiting_instead_of_failing(self):
        """★ 设备还在忙（Processing / 有未完成 job）⇒ **继续等**，不误报失败

        ⇒ 这是"用户在打印机面板上还没按确认键"的场景：
        设备暂时给不出文档，但作业还在 ⇒ 不能报失败，更不能报"取消"。
        """
        # 先连续几个 404（设备还没吐数据），随后吐出一页 ⇒ 必须成功
        session = _FakeSession([_Resp(404)] * 5 + [_Resp(200, content=b"P1"),
                                                    _Resp(404)])
        result = _run_multipage(session, BUSY, source="Feeder")
        assert [d for d, _e in result] == [b"P1"], "设备忙的时候必须继续等"

    def test_never_returns_empty_list_on_failure(self):
        """★ 终审：这条路径**绝不**返回空列表（空列表 = 取消 = 误导）"""
        session = _FakeSession([_Resp(404)] * 40)
        try:
            result = _run_multipage(session, IDLE, source="Feeder")
        except escl_engine.ScanDeviceError:
            return                      # 抛错 = 正确
        assert result, "失败时返回了空列表 ⇒ 会被当成『取消』"
        raise AssertionError("应当抛 ScanDeviceError")


class TestCancelSemanticsUnchanged:
    """真·取消的语义**不能**被这次改动影响"""

    def test_cancel_during_polling_returns_partial_pages(self):
        cancel = threading.Event()
        session = _FakeSession([
            _Resp(200, content=b"P1"),
            lambda s, u: (cancel.set(), _Resp(404))[1],
        ])
        clock = _FakeClock()
        with mock.patch.object(escl_engine, "time", clock), \
                mock.patch.object(escl_engine.requests, "Session",
                                  return_value=session), \
                mock.patch.object(escl_engine, "probe_scanner_status",
                                  return_value=IDLE):
            result = escl_engine.execute_multipage_scan(
                scanner=_scanner(), source="Feeder", cancel_event=cancel)
        assert [d for d, _e in result] == [b"P1"], "取消时应返回已取到的部分页"

    def test_cancel_before_first_page_returns_empty(self):
        """一页都没取到就取消 ⇒ 返回空列表（协调器据此报"已取消"）"""
        cancel = threading.Event()
        cancel.set()
        session = _FakeSession([])
        clock = _FakeClock()
        with mock.patch.object(escl_engine, "time", clock), \
                mock.patch.object(escl_engine.requests, "Session",
                                  return_value=session), \
                mock.patch.object(escl_engine, "probe_scanner_status",
                                  return_value=IDLE):
            result = escl_engine.execute_multipage_scan(
                scanner=_scanner(), source="Feeder", cancel_event=cancel)
        assert result == [], "真取消 ⇒ 空列表（这是协调器认的『取消』信号）"


class TestNoDocumentMessageHelper:
    def test_feeder_and_platen_are_different(self):
        feeder = escl_engine._no_document_message("Feeder")
        platen = escl_engine._no_document_message("Platen")
        assert feeder != platen
        assert "输稿器" in feeder
        assert "平板" in platen

    def test_jam_state_overrides_source(self):
        msg = escl_engine._no_document_message("Feeder", "ScannerAdfJam")
        assert "卡纸" in msg

    def test_non_jam_state_uses_source_message(self):
        msg = escl_engine._no_document_message("Feeder", "ScannerAdfEmpty")
        assert "卡纸" not in msg
        assert "输稿器" in msg


@pytest.fixture(autouse=True)
def _isolated_appdata(tmp_path, monkeypatch):
    """★ 收尾在取不到文档时会把作业记进待清清单
    （%APPDATA%\\HP_Scan_Tool\\pending_jobs.json）
    ⇒ 测试必须隔离 APPDATA，否则会污染**用户真实**的清单
    （清单里若留下真机 URI，下次运行会真的去设备上取那个作业）。"""
    appdata = tmp_path / "appdata"
    (appdata / "HP_Scan_Tool").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    yield appdata
