"""
eSCL 错误文案 / 退避 / 传输层失败契约回归测试 — HP Scan Tool v4.0

锁死三条用户可感知契约：

  P3-a（503 文案 + 退避）
    - 503 的文案必须**按来源**区分：平板(Platen)不能说"去输稿器放纸"
      （本次请求的 InputSource 决定原因，指错方向 = 用户白跑一趟）；
    - 503 必须有**有界**退避重试（与 409 同一套写法：有限次数 + 有限退避 +
      等待可被用户取消），最终仍失败才抛可读错误。

  P3-b（传输层失败的中文指引）
    - requests 的原始异常（urllib3 英文长串）不得原样冒泡给 GUI；
      必须包成 `ScanDeviceError` + 中文可执行指引，**根因保留在 __cause__**。

  取消语义不被搞混
    - `escl_engine.ScanCancelled` / `ScanJobCancelled` 与
      `scan_coordinator.ScanCancelled` 的继承关系**不得**被本次改动破坏。

★ 全程 mock：**不发任何真实 HTTP**（不碰 17.17.* 真机），不写真实用户数据。
"""

import threading
from unittest import mock

import pytest
import requests

import escl_engine
from escl_engine import ScannerInfo, ScanDeviceError


class _Resp:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _scanner():
    s = ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    return s


def _create(session, source="Platen", cancel_event=None):
    """走真实的 `_create_scan_job`（唯一改动面），session 全 mock。"""
    return escl_engine._create_scan_job(
        session, _scanner(), 300, "RGB24", "jpg", source, False,
        cancel_event=cancel_event)


# ── P3-a：503 文案按来源区分 ─────────────────────────────────


class TestServiceUnavailableMessageIsSourceAware:
    def test_platen_503_never_points_at_feeder(self):
        """平板 503 ⇒ 文案不得出现「输稿器」。

        旧文案只有一句「若选择的是输稿器(ADF)扫描，请确认输稿器里已放入纸张
        后重试」—— 本次请求的 InputSource 是 Platen 时，这句话把用户指向了
        错误的原因（用户会去白跑输稿器）。
        """
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        msg = str(exc.value)
        assert "503" in msg, f"必须写明 HTTP 状态码: {msg}"
        assert "输稿器" not in msg, \
            f"平板 503 的文案不得把用户指向输稿器: {msg}"
        assert "设备尚未就绪" in msg, f"必须写清真实原因: {msg}"
        assert "稍候重试" in msg, f"必须给出下一步动作: {msg}"

    def test_feeder_503_keeps_paper_hint(self):
        """输稿器 503 ⇒ 保留「确认已放纸」的意思。"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Feeder")

        msg = str(exc.value)
        assert "503" in msg
        assert "输稿器" in msg, f"输稿器 503 必须提到输稿器: {msg}"
        assert "纸" in msg, f"输稿器 503 必须保留放纸指引: {msg}"

    def test_503_message_does_not_leak_conflict_wording(self):
        """503 与 409 是两种状态，文案不得互相污染。"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session)

        msg = str(exc.value)
        assert "409" not in msg
        assert "15-20 分钟" not in msg


# ── P3-a：503 有界退避重试 ────────────────────────────────────


class TestServiceUnavailableRetries:
    def test_503_is_retried_and_backoff_is_bounded(self):
        """503 必须重试，且次数与退避时长都有界。"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(ScanDeviceError):
                _create(session)

        posts = session.post.call_count
        total_wait = sum(c.args[0] for c in sleeper.call_args_list)
        assert posts > 1, f"503 必须至少重试一次（实际 {posts} 次）"
        assert posts <= 4, f"503 重试次数必须有界（实际 {posts} 次）"
        assert 0 < total_wait <= 15.0, \
            f"503 退避总时长必须有界（实际 {total_wait}s）"

    def test_503_retry_succeeds_when_device_becomes_ready(self):
        """设备收尾完成后重试应当成功（这正是 503 退避的目的）。"""
        session = mock.MagicMock()
        session.post.side_effect = [
            _Resp(503, ""),
            _Resp(503, ""),
            _Resp(201, "", {"Location": "http://1.2.3.4:80/eSCL/ScanJobs/7"}),
        ]
        with mock.patch("escl_engine.time.sleep"):
            _base, job_uri, ext = _create(session)

        assert job_uri.endswith("/7")
        assert ext
        assert session.post.call_count == 3

    def test_503_backoff_wait_is_cancellable(self):
        """退避等待期间用户取消 ⇒ 立刻中断，不再重试。"""
        ev = threading.Event()
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")

        def fake_sleep(seconds):
            ev.set()          # 第一次等待就收到取消信号

        with mock.patch("escl_engine.time.sleep", side_effect=fake_sleep):
            with pytest.raises(escl_engine.ScanJobCancelled):
                _create(session, cancel_event=ev)

        assert session.post.call_count == 1, "取消后不应再重试"

    def test_cancelled_is_not_reported_as_device_error(self):
        """取消 ≠ 失败：ScanJobCancelled 不得被当成设备错误。"""
        assert issubclass(escl_engine.ScanJobCancelled, escl_engine.ScanCancelled)
        assert not issubclass(escl_engine.ScanJobCancelled, ScanDeviceError)


# ── P3-b：传输层失败 ⇒ 中文可执行指引 ─────────────────────────


class TestTransportErrorsBecomeActionableChineseErrors:
    def test_connection_error_is_wrapped_with_chinese_guidance(self):
        session = mock.MagicMock()
        session.post.side_effect = requests.ConnectionError(
            "HTTPConnectionPool(host='127.0.0.1', port=1): Max retries exceeded "
            "with url: /eSCL/ScanJobs (Caused by NewConnectionError(...))")
        with pytest.raises(ScanDeviceError) as exc:
            _create(session)

        msg = str(exc.value)
        assert "无法连接打印机" in msg, f"必须给出中文可执行指引: {msg}"
        assert "同一网络" in msg, f"必须写清下一步动作: {msg}"
        assert "Max retries exceeded" not in msg, \
            f"不得把 urllib3 英文长串甩给用户: {msg}"
        assert isinstance(exc.value.__cause__, requests.ConnectionError), \
            "原始异常必须保留在 __cause__（根因不许丢）"

    def test_timeout_is_wrapped_with_chinese_guidance(self):
        session = mock.MagicMock()
        session.post.side_effect = requests.ReadTimeout("timed out")
        with pytest.raises(ScanDeviceError) as exc:
            _create(session)

        msg = str(exc.value)
        assert "打印机" in msg, f"必须给出中文可执行指引: {msg}"
        assert "超时" in msg, f"必须写清是超时: {msg}"
        assert isinstance(exc.value.__cause__, requests.ReadTimeout), \
            "原始异常必须保留在 __cause__"

    def test_execute_scan_surfaces_chinese_error(self):
        """端到端（仍全 mock）：execute_scan 的建 job 请求断连 ⇒ 中文指引。"""
        session = mock.MagicMock()
        session.post.side_effect = requests.ConnectionError("connection refused")
        with mock.patch("escl_engine.requests.Session", return_value=session):
            with pytest.raises(ScanDeviceError) as exc:
                escl_engine.execute_scan(scanner=_scanner(), source="Platen")

        msg = str(exc.value)
        assert "无法连接打印机" in msg
        assert "Max retries" not in msg

    def test_other_request_exceptions_are_wrapped_too(self):
        """requests.RequestException 的其它合理子类同样包成可操作错误。"""
        session = mock.MagicMock()
        session.post.side_effect = requests.exceptions.TooManyRedirects("loop")
        with pytest.raises(ScanDeviceError) as exc:
            _create(session)
        assert "无法连接打印机" in str(exc.value)
        assert isinstance(exc.value.__cause__, requests.RequestException)


# ── P3-b：GUI / CLI 侧看到的必须是中文指引 ────────────────────


class TestGuiAndCliSeeChineseGuidance:
    def test_coordinator_scan_failed_payload_is_chinese(self):
        """协调器把 `str(e)` 原样发给 GUI（hp_scan_gui_pyside 的弹窗文案）。

        这里用**最小替身**驱动协调器的 `_ScanTask.run`，断言 `scan_failed`
        带出去的字符串就是中文指引 —— 即 GUI 弹窗里用户真正看到的那一串。
        """
        from PySide6.QtCore import QObject, Signal

        import scan_coordinator

        class _StubCoordinator(QObject):
            scan_started = Signal(str)
            scan_finished = Signal(str, object)
            scan_failed = Signal(str, str)
            scan_cancelled = Signal(str)

            def __init__(self, exc):
                super().__init__()
                self._exc = exc

            def run_scan_sync(self, *args, **kwargs):
                raise self._exc

            def _clear_cancel_event(self, job_id):
                pass

        exc = ScanDeviceError(
            "无法连接打印机，请确认已开机并与本机在同一网络")
        coord = _StubCoordinator(exc)
        received = []
        coord.scan_failed.connect(
            lambda job_id, err: received.append((job_id, err)))

        task = scan_coordinator._ScanTask(
            coord, None, {}, "job-1", threading.Event())
        task.run()

        assert received, "失败必须发 scan_failed 信号"
        _job_id, payload = received[0]
        assert payload == str(exc)
        assert "无法连接打印机" in payload
        assert "同一网络" in payload

    def test_cli_keeps_actionable_path_for_new_error(self):
        """CLI 侧不得变差：仍是「可操作错误」⇒ 不甩 traceback。"""
        import cli

        exc = ScanDeviceError(
            "无法连接打印机，请确认已开机并与本机在同一网络")
        assert cli._is_actionable_device_error(exc) is True

    def test_cli_reports_connection_failure_without_traceback(self, capsys):
        import cli

        exc = ScanDeviceError(
            "无法连接打印机，请确认已开机并与本机在同一网络")
        cli._report_scan_error(exc)
        captured = capsys.readouterr()
        assert "无法连接打印机" in captured.err
        assert "Traceback" not in captured.err, \
            "可操作错误不得甩 traceback"


# ── 取消语义的继承关系不被破坏 ────────────────────────────────


class TestCancelSemanticsUnchanged:
    def test_cancel_inheritance_chain(self):
        import scan_coordinator

        assert issubclass(scan_coordinator.ScanCancelled,
                          escl_engine.ScanCancelled)
        assert issubclass(escl_engine.ScanJobCancelled,
                          escl_engine.ScanCancelled)
