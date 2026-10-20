"""
eSCL 409 退避与格式契约回归测试 — HP Scan Tool v4.0

锁死两条 finding：
  m-4（Minor）：409 退避窗口失配且不可取消
       - 设备侧残留 job 的 TTL 实测 15-20 分钟，而退避只有 2/4/8 秒共 14 秒
         ⇒ 重试成功概率≈0，只是把错误提示推迟 14 秒
       - time.sleep 不可取消 ⇒ 用户点取消后最多再等 14 秒
       修法：有界退避 + 明确告知 TTL + 分片可取消等待；503 分支保持独立。
  m-6（Minor）：--format bmp 静默降级
       修法：CLI 去掉 bmp（显式报错）；协议层对不支持的格式显式抛错。
"""

import threading
import time
from unittest import mock

import pytest

import escl_engine
from escl_engine import ScannerInfo


class _Resp:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _scanner():
    s = ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    return s


def _create(session, output_format="jpg", cancel_event=None):
    return escl_engine._create_scan_job(
        session, _scanner(), 300, "RGB24", output_format, "Platen", False,
        cancel_event=cancel_event)


class TestConflictBackoffIsCancellable:
    def test_cancel_during_backoff_aborts_promptly(self):
        """退避期间取消 ⇒ 立刻中断，不再重试"""
        ev = threading.Event()
        session = mock.MagicMock()
        session.post.return_value = _Resp(409, "busy")

        def fake_sleep(seconds):
            ev.set()          # 第一次等待就收到取消信号

        started = time.monotonic()
        with mock.patch("escl_engine.time.sleep", side_effect=fake_sleep):
            with pytest.raises(escl_engine.ScanJobCancelled):
                _create(session, cancel_event=ev)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"取消后应当立刻返回，实际耗时 {elapsed:.2f}s"
        assert session.post.call_count == 1, "取消后不应再重试"

    def test_cancel_before_backoff_never_sleeps(self):
        """进入退避前就已取消 ⇒ 一次都不睡"""
        ev = threading.Event()
        ev.set()
        session = mock.MagicMock()
        session.post.return_value = _Resp(409, "busy")

        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(escl_engine.ScanJobCancelled):
                _create(session, cancel_event=ev)

        assert not sleeper.called, "已取消就不该再睡"
        assert session.post.call_count == 1

    def test_backoff_is_sliced_when_cancellable(self):
        """有取消信号时，等待必须切成小片（每片 ≤0.2 秒）"""
        ev = threading.Event()
        session = mock.MagicMock()
        session.post.return_value = _Resp(409, "busy")

        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(RuntimeError):
                _create(session, cancel_event=ev)

        assert sleeper.call_count > 3, "退避应当被切成多片"
        assert all(c.args[0] <= 0.2 for c in sleeper.call_args_list), \
            "每片等待必须 ≤0.2 秒，取消才来得及生效"
        assert sum(c.args[0] for c in sleeper.call_args_list) == pytest.approx(14.0)

    def test_without_cancel_event_single_sleep(self):
        """没有取消信号时仍是一次性 sleep（保持既有调用形态）"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(409, "busy")

        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(RuntimeError):
                _create(session)

        assert [c.args[0] for c in sleeper.call_args_list] == [2, 4, 8]


class TestConflictMessageTellsTtl:
    def test_message_mentions_ttl_and_action(self):
        """错误文案必须写清设备回收要多久 + 用户能做什么"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(409, "busy")

        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(RuntimeError) as exc:
                _create(session)

        msg = str(exc.value)
        assert "409" in msg
        assert "未完成" in msg
        assert "15-20 分钟" in msg, f"必须写清 TTL 等待时间: {msg}"
        assert "打印机面板" in msg, f"必须给出可执行动作: {msg}"
        assert "稍后重试" in msg

    def test_ttl_hint_constant_matches_message(self):
        assert escl_engine._CONFLICT_TTL_HINT in escl_engine._CONFLICT_MESSAGE
        assert "15-20 分钟" in escl_engine._CONFLICT_TTL_HINT


class TestServiceUnavailableUnaffected:
    def test_503_is_not_retried(self):
        """503（输稿器空载）是独立分支：不重试、不退避"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")

        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(RuntimeError) as exc:
                _create(session)

        msg = str(exc.value)
        assert "503" in msg
        assert "输稿器" in msg
        assert session.post.call_count == 1, "503 不得重试"
        assert not sleeper.called, "503 不得退避等待"

    def test_409_and_503_stay_separate(self):
        """409 与 503 的文案不得互相污染"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(RuntimeError) as exc:
                _create(session)
        msg = str(exc.value)
        assert "409" not in msg
        assert "15-20 分钟" not in msg


class TestUnsupportedFormatIsExplicit:
    def test_bmp_is_rejected_not_downgraded(self):
        """不支持的格式必须显式报错，不能静默回退成 jpeg"""
        session = mock.MagicMock()

        with pytest.raises(RuntimeError) as exc:
            _create(session, output_format="bmp")

        msg = str(exc.value)
        assert "bmp" in msg
        assert "不支持" in msg
        assert session.post.call_count == 0, "格式不支持就不该发请求"

    def test_supported_formats_still_work(self):
        for fmt in ("jpg", "jpeg", "png", "tiff", "tif", "pdf"):
            session = mock.MagicMock()
            session.post.return_value = _Resp(
                201, "", {"Location": "http://1.2.3.4:80/eSCL/ScanJobs/7"})
            _base, job_uri, ext = _create(session, output_format=fmt)
            assert job_uri.endswith("/7")
            assert ext

    def test_request_format_has_no_bmp(self):
        assert "bmp" not in escl_engine.REQUEST_FORMAT
