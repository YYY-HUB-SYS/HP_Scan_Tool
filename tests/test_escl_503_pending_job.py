"""
503 的"设备上有未完成 ScanJob"指引 + 退避档位契约 — HP Scan Tool v4.0

★ 2026-10-21 真机复验推翻了上一轮（提交 a943d93 / 881f203）的判据：
  上一轮认为 503 = "设备还在收尾上一个 job" ⇒ 平板退避 2+4+8（14 秒）。
  真机两轮实测证明：**503 的主要成因是"设备上存在一个未完成的 ScanJob"**
  （残留的、或正在处理的），**不是**"设备还在收尾上一个 job"：
    - 第一轮：探针把上一个 job 留在设备上没删 ⇒ 设备状态 Processing、
      ScannerStatus 里该 job 仍在、ImagesToTransfer=1 ⇒ 建新 job 持续 503，
      **退避 14 秒也没恢复**（残留记录实测 30 天都不回收）；
    - 第二轮：DELETE 正确执行后（设备回 Idle）⇒ 紧接着建 job **2.27 秒就成功**，
      压根没收到 503。
  ⇒ 对"残留 job"这个主要场景，旧文案"请稍候重试"是**无效指引**（干等没用），
    用户真正该做的是"**到打印机面板上取消那个任务**"；
  ⇒ 对"瞬时收尾"，实测只需 2.27 秒 ⇒ 14 秒退避**偏长**。

本文件锁死六条契约：
  ① 503 之后要查设备状态；查到有未完成 job ⇒ 文案必须指向"打印机面板/取消"；
  ② **查到未完成 job ⇒ 立即报错、不再退避**（设备上有未完成 job 时建新 job
     必然失败 ⇒ 继续退避只是白等）；平板与输稿器一视同仁；
  ③ 查不到 / 查询本身失败 ⇒ 按来源走原有退避（平板 2+4=6 秒；输稿器不退避），
     退避用尽后再抛原来的错（平板「请稍候重试」/ 输稿器「放纸」）；
  ④ 探测必须容错（超时/异常/XML 畸形都不得让用户看到崩溃），且**只在收到
     503 之后**才探（正常路径不多发请求）；整条失败路径**只探一次**；
  ⑤ 退避档位 = 2+4（6 秒）：平板退避、输稿器不退试；
  ⑥ 探测**不破坏可取消**：退避等待期间用户点取消能立刻中断。

★ 全程 mock：不发任何真实 HTTP（不碰 17.17.* 真机），不写真实用户数据。
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


# 设备上**残留一个未完成 job** 时的 ScannerStatus（真机实测形态的最小复刻）：
#   设备状态 Processing + scan:Jobs 里那个 job 仍在 + 还有 1 张图没传走。
_STATUS_PENDING_JOB = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:State>Processing</pwg:State>
  <scan:AdfState>ScannerAdfEmpty</scan:AdfState>
  <scan:Jobs>
    <scan:JobInfo>
      <scan:JobUri>http://192.168.1.50:80/eSCL/ScanJobs/1</scan:JobUri>
      <scan:JobState>Processing</scan:JobState>
      <scan:JobImagesToTransfer>1</scan:JobImagesToTransfer>
    </scan:JobInfo>
  </scan:Jobs>
</scan:ScannerStatus>"""

# 残留 job 已被 DELETE，但设备还没收尾完（真机第二轮的形态）：
#   job 状态已 Canceled、没有待传图像 ⇒ **不该**让用户去面板取消。
_STATUS_CANCELED_JOB = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:State>Processing</pwg:State>
  <scan:AdfState>ScannerAdfEmpty</scan:AdfState>
  <scan:Jobs>
    <scan:JobInfo>
      <scan:JobUri>http://192.168.1.50:80/eSCL/ScanJobs/1</scan:JobUri>
      <scan:JobState>Canceled</scan:JobState>
      <scan:JobImagesToTransfer>0</scan:JobImagesToTransfer>
    </scan:JobInfo>
  </scan:Jobs>
</scan:ScannerStatus>"""

# 已完成的历史 job（Completed + 没有待传图像）⇒ 设备是干净的，不算阻塞。
_STATUS_COMPLETED_JOB = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:State>Idle</pwg:State>
  <scan:AdfState>ScannerAdfEmpty</scan:AdfState>
  <scan:Jobs>
    <scan:JobInfo>
      <scan:JobUri>http://192.168.1.50:80/eSCL/ScanJobs/7</scan:JobUri>
      <scan:JobState>Completed</scan:JobState>
      <scan:JobImagesToTransfer>0</scan:JobImagesToTransfer>
    </scan:JobInfo>
  </scan:Jobs>
</scan:ScannerStatus>"""

# 设备完全空闲（没有 scan:Jobs 节点）。
_STATUS_IDLE = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:State>Idle</pwg:State>
  <scan:AdfState>ScannerAdfEmpty</scan:AdfState>
</scan:ScannerStatus>"""


def _scanner():
    s = ScannerInfo(name="test", ip="192.168.1.50")
    s.escl_url = "http://192.168.1.50:80/eSCL/"
    return s


def _create(session, source="Platen", cancel_event=None):
    """走真实的 `_create_scan_job`（唯一改动面），session 全 mock。"""
    return escl_engine._create_scan_job(
        session, _scanner(), 300, "RGB24", "jpg", source, False,
        cancel_event=cancel_event)


def _session_with_status(status_xml, post_status=503):
    """POST 一律 503；GET /ScannerStatus 返回给定的状态 XML。"""
    session = mock.MagicMock()
    session.post.return_value = _Resp(post_status, "")
    session.get.return_value = _Resp(200, status_xml)
    return session


# ── 契约①②：设备上确有未完成 job ⇒ 指向"打印机面板/取消" + 不再退避 ──


class TestPendingJobGuidance:
    def test_pending_job_points_user_to_printer_panel(self):
        """设备上有未完成 job ⇒ 文案必须让用户去打印机面板取消它。"""
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        msg = str(exc.value)
        assert "503" in msg, f"必须写明 HTTP 状态码: {msg}"
        assert "未完成" in msg, f"必须写明是未完成的扫描任务: {msg}"
        assert "打印机面板" in msg, f"必须指向打印机面板: {msg}"
        assert "取消" in msg, f"必须给出可执行动作（取消）: {msg}"

    def test_pending_job_message_drops_the_useless_wait_hint(self):
        """有未完成 job 时**不得**再说"请稍候重试"—— 干等 14 秒也没用。

        这正是本次改动的核心：旧文案在残留 job 场景下是**无效指引**。
        """
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        msg = str(exc.value)
        assert "稍候重试" not in msg, \
            f"有未完成 job 时不得再让用户干等: {msg}"

    def test_platen_pending_message_never_points_at_feeder(self):
        """平板 503 的文案（无论查不查得到 job）都不得出现「输稿器」。"""
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        msg = str(exc.value)
        assert "输稿器" not in msg, f"平板 503 文案不得指向输稿器: {msg}"

    def test_feeder_pending_message_keeps_paper_hint(self):
        """输稿器 + 有未完成 job ⇒ 两件事都要说：先取消任务，纸也还是要放。

        输稿器空载确实是 503 的成因之一 ⇒ "放纸"指引不能丢。
        """
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Feeder")

        msg = str(exc.value)
        assert "打印机面板" in msg, f"必须指向打印机面板: {msg}"
        assert "取消" in msg, f"必须给出可执行动作（取消）: {msg}"
        assert "输稿器" in msg, f"输稿器 503 必须提到输稿器: {msg}"
        assert "纸" in msg, f"输稿器 503 必须保留放纸指引: {msg}"
        assert "放纸后请重试" in msg, f"必须明确告诉用户放纸后重试: {msg}"

    def test_canceled_job_without_pending_images_is_not_blocking(self):
        """已 Canceled 且没有待传图像的 job ⇒ 设备只是在收尾 ⇒ 退回"稍候重试"。"""
        session = _session_with_status(_STATUS_CANCELED_JOB)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        msg = str(exc.value)
        assert "稍候重试" in msg, f"必须给出下一步动作: {msg}"
        assert "取消该任务" not in msg, \
            f"没有未完成 job 就不得让用户去取消: {msg}"

    def test_completed_job_is_not_blocking(self):
        """已完成的历史 job ⇒ 不算阻塞 ⇒ 退回"稍候重试"。"""
        session = _session_with_status(_STATUS_COMPLETED_JOB)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        msg = str(exc.value)
        assert "稍候重试" in msg, f"必须给出下一步动作: {msg}"
        assert "取消该任务" not in msg, \
            f"没有未完成 job 就不得让用户去取消: {msg}"


# ── 契约②：查到未完成 job ⇒ 立即报错、**不再退避** ────────────────


class TestPendingJobStopsTheBackoff:
    """设备上有未完成 job 时，建新 job **必然失败**（真机实测：退避 14 秒
    也没恢复）⇒ 继续退避是**白等** ⇒ 必须立刻告诉用户去面板取消那个任务。

    这一组断言专门抓"又退避了"的回归。
    """

    def test_platen_pending_job_raises_without_any_retry(self):
        """平板 + 残留 job ⇒ **一次都不该重试**（POST 只发 1 次）。"""
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        assert session.post.call_count == 1, \
            (f"查到未完成 job 后不得再重试（实际 POST "
             f"{session.post.call_count} 次）")
        assert not sleeper.called, \
            f"查到未完成 job 后不得再退避等待（实际睡了 {sleeper.call_count} 次）"
        assert "取消" in str(exc.value)

    def test_platen_pending_job_does_not_wait_the_full_backoff(self):
        """平板 + 残留 job ⇒ 不得走满 2+4 的退避（等待时长必须为 0）。"""
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(ScanDeviceError):
                _create(session, source="Platen")

        waited = sum(c.args[0] for c in sleeper.call_args_list)
        assert waited == 0.0, f"查到未完成 job 后等待时长必须为 0（实际 {waited}s）"

    def test_feeder_pending_job_also_raises_without_waiting(self):
        """输稿器 + 残留 job ⇒ 同样立即报错（不靠"输稿器不重试"顺带满足）。"""
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Feeder")

        assert session.post.call_count == 1, \
            f"查到未完成 job 后不得再重试（实际 {session.post.call_count} 次）"
        assert not sleeper.called, "查到未完成 job 后不得再退避等待"
        assert "打印机面板" in str(exc.value)

    def test_pending_job_probe_happens_once(self):
        """整条失败路径**只探一次**状态（不是每个退避片都探）。"""
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError):
                _create(session, source="Platen")

        assert session.get.call_count == 1, \
            f"只许探一次状态（实际 {session.get.call_count} 次）"


# ── 契约③：查不到 / 查失败 ⇒ 按来源走原有退避 ─────────────────────


class TestFallbackWhenStatusUnknown:
    def test_idle_device_keeps_wait_and_retry_hint(self):
        """设备上没有 job ⇒ 保留"请稍候重试"。"""
        session = _session_with_status(_STATUS_IDLE)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        msg = str(exc.value)
        assert "稍候重试" in msg, f"必须给出下一步动作: {msg}"
        assert "取消该任务" not in msg, \
            f"没有未完成 job 就不得让用户去取消: {msg}"

    def test_idle_device_still_uses_source_backoff(self):
        """查不到未完成 job ⇒ 平板照旧退避 2+4（6 秒）。"""
        session = _session_with_status(_STATUS_IDLE)
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(ScanDeviceError):
                _create(session, source="Platen")

        delays = [c.args[0] for c in sleeper.call_args_list]
        assert delays == [2.0, 4.0], f"退避入参必须逐个锁定: {delays}"
        assert session.post.call_count == 3, \
            f"平板 503 = 1 次 + 2 次退避（实际 {session.post.call_count} 次）"

    def test_status_query_timeout_falls_back(self):
        """查状态超时 ⇒ 退回旧文案 + 照旧退避，**不得**让用户看到崩溃。"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        session.get.side_effect = requests.Timeout("timed out")
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        msg = str(exc.value)
        assert "稍候重试" in msg, f"必须给出下一步动作: {msg}"
        assert "Timeout" not in msg, f"不得把英文异常甩给用户: {msg}"
        assert [c.args[0] for c in sleeper.call_args_list] == [2.0, 4.0], \
            "探测失败后仍须按来源退避"

    def test_status_query_broken_connection_falls_back(self):
        """查状态连不上 ⇒ 退回旧文案。"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        session.get.side_effect = requests.ConnectionError("refused")
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        assert "稍候重试" in str(exc.value)

    def test_status_query_unexpected_exception_falls_back(self):
        """查状态抛出**任何**意外异常 ⇒ 吞掉并退回旧文案（绝不当成崩溃）。"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        session.get.side_effect = RuntimeError("boom")
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        assert "稍候重试" in str(exc.value)

    def test_malformed_status_xml_falls_back(self):
        """状态 XML 畸形 ⇒ 退回旧文案。"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        session.get.return_value = _Resp(200, "<not-xml")
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        assert "稍候重试" in str(exc.value)

    def test_status_query_non_200_falls_back(self):
        """状态查询非 200 ⇒ 退回旧文案。"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        session.get.return_value = _Resp(500, "")
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError) as exc:
                _create(session, source="Platen")

        assert "稍候重试" in str(exc.value)


# ── 契约④：探测的时机 / 次数 / 超时 / 开销 ───────────────────────


class TestStatusQueryTiming:
    def test_probe_happens_right_after_the_first_503(self):
        """探测必须发生在**第一次 503 之后、且在第一次退避之前**。

        顺序即契约：post → get（探测）→ post → post（退避重试）。
        """
        session = _session_with_status(_STATUS_IDLE)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError):
                _create(session, source="Platen")

        names = [c[0] for c in session.mock_calls]
        assert names[0] == "post", f"第一个请求必须是建 job: {names}"
        assert names[:2] == ["post", "get"], \
            f"探测必须在第一次 503 之后立刻发生: {names}"
        assert names.count("get") == 1, \
            f"整条失败路径只许查一次状态（实际 {names.count('get')} 次）"

    def test_success_path_makes_no_status_query(self):
        """建 job 成功 ⇒ **一次状态查询都不许有**（不给正常路径加开销）。"""
        session = mock.MagicMock()
        session.post.return_value = _Resp(
            201, "", {"Location": "http://192.168.1.50:80/eSCL/ScanJobs/7"})
        _base, job_uri, ext = _create(session, source="Platen")

        assert job_uri.endswith("/7")
        assert session.get.call_count == 0, \
            f"正常路径不得发状态查询（实际 {session.get.call_count} 次）"

    def test_status_query_timeout_is_short(self):
        """状态查询超时必须很短（≤ 3 秒）—— 不得变成阻塞式的长时间等待。"""
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError):
                _create(session, source="Platen")

        assert session.get.call_count == 1
        timeout = session.get.call_args.kwargs.get("timeout")
        assert timeout is not None, "状态查询必须带超时"
        assert timeout <= 3.0, f"状态查询超时必须 ≤ 3 秒（实际 {timeout}s）"
        assert timeout > 0

    def test_status_query_url_is_scanner_status(self):
        """状态查询走的就是既有的 /ScannerStatus 端点。"""
        session = _session_with_status(_STATUS_PENDING_JOB)
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(ScanDeviceError):
                _create(session, source="Platen")

        url = session.get.call_args.args[0]
        assert url.endswith("/ScannerStatus"), f"端点不对: {url}"


# ── 契约⑤⑥：退避档位 2+4（6 秒）+ 可取消不被破坏 ──────────────────


class TestBackoffLadder:
    def test_backoff_ladder_is_two_and_four(self):
        """平板 503（查不到未完成 job）⇒ 退避档位必须是 2+4（6 秒）。"""
        session = _session_with_status(_STATUS_IDLE)
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(ScanDeviceError):
                _create(session, source="Platen")

        delays = [c.args[0] for c in sleeper.call_args_list]
        assert delays == [2.0, 4.0], f"退避入参必须逐个锁定: {delays}"
        assert sum(delays) == pytest.approx(6.0)
        assert session.post.call_count == 3, \
            f"平板 503 = 1 次 + 2 次退避（实际 {session.post.call_count} 次）"

    def test_feeder_is_not_retried(self):
        """输稿器 503 ⇒ 仍然不重试（按来源分流不被破坏）。"""
        session = _session_with_status(_STATUS_IDLE)
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(ScanDeviceError):
                _create(session, source="Feeder")

        assert session.post.call_count == 1, \
            f"输稿器 503 不得重试（实际 {session.post.call_count} 次）"
        assert not sleeper.called, "输稿器 503 不得退避等待"

    def test_wait_is_still_cancellable(self):
        """退避等待仍必须可被用户取消（分片等待不被破坏）。"""
        ev = threading.Event()
        session = _session_with_status(_STATUS_IDLE)

        def fake_sleep(seconds):
            ev.set()          # 第一次等待就收到取消信号

        with mock.patch("escl_engine.time.sleep", side_effect=fake_sleep):
            with pytest.raises(escl_engine.ScanJobCancelled):
                _create(session, source="Platen", cancel_event=ev)

        assert session.post.call_count == 1, "取消后不应再重试"

    def test_cancel_during_backoff_aborts_promptly(self):
        """退避期间取消 ⇒ 立刻中断，不再重试（探测已在第一次 503 时做过）。"""
        ev = threading.Event()
        session = _session_with_status(_STATUS_IDLE)

        def fake_sleep(seconds):
            ev.set()

        with mock.patch("escl_engine.time.sleep", side_effect=fake_sleep):
            with pytest.raises(escl_engine.ScanJobCancelled):
                _create(session, source="Platen", cancel_event=ev)

        assert session.post.call_count == 1, "取消后不应再重试"
        assert session.get.call_count == 1, \
            f"探测只发生一次（实际 {session.get.call_count} 次）"

    def test_cancel_before_probe_skips_the_status_query(self):
        """用户已取消 ⇒ 不必再花一次 HTTP 去查状态。"""
        ev = threading.Event()
        ev.set()
        session = _session_with_status(_STATUS_IDLE)

        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(escl_engine.ScanJobCancelled):
                _create(session, source="Platen", cancel_event=ev)

        assert session.get.call_count == 0, \
            f"已取消就不该再查状态（实际 {session.get.call_count} 次）"
        assert session.post.call_count == 1, "取消后不应再重试"


# ── 既有契约不被破坏 ────────────────────────────────────────────


class TestExistingContractsUnbroken:
    def test_get_scanner_status_contract_unchanged(self):
        """get_scanner_status 的**原有键**不得变（cli.py cmd_status 依赖它）。

        ★ 2026-10-22：新增 `adf_state`（AdfState 原值）⇒ 键集合多一个。
          ★ **cli.py 不受影响**：它用的是 `if "adf_loaded" in status` 这种
            **存在性**判断，不是精确键集合。
        """
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(200, _STATUS_IDLE)):
            st = escl_engine.get_scanner_status("http://192.168.1.50/eSCL/")
        assert {"state", "adf_loaded"} <= set(st.keys())

    def test_probe_scanner_status_default_contract_unchanged(self):
        """默认调用 probe_scanner_status 必须带上**原有键**（保持既有契约）。

        ★ 2026-10-22：额外多一个 `adf_state`（AdfState 原值，用来区分
          「没纸」与「卡纸」）；`include_jobs=True` 时的既有键不受影响。
        """
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(200, _STATUS_IDLE)):
            ok, st = escl_engine.probe_scanner_status("http://192.168.1.50/eSCL/")
        assert ok is True
        assert {"state", "adf_loaded"} <= set(st.keys())

    def test_escl_engine_does_not_import_qt(self):
        """escl_engine 不得 import Qt（CLI 也要能用它）。"""
        import pathlib
        src = pathlib.Path(escl_engine.__file__).read_text(encoding="utf-8")
        for bad in ("import PySide6", "from PySide6", "import PyQt5", "from PyQt5"):
            assert bad not in src, f"escl_engine 不得引入 Qt: {bad}"
