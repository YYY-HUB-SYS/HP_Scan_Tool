"""
409 判据契约测试 — HP Scan Tool v4.0

锁死 **2026-10-22 四台真机逐台验出来的判据**（这一条此前被我判错过两次）：

| 设备 | 设备侧作业记录 | 有没有 `Completed` | `POST /eSCL/ScanJobs` |
|---|---|---|---|
| `.101`（OfficeJet Pro 7720） | `Completed` + `Completed` | **有** | **201** ✔ |
| `.203`（M232） | `Canceled` ×2 | **没有** | **409** ✘ |
| `.208`（M232） | `Canceled` ×2 | **没有** | **409** ✘ |
| `.111`（M232） | `Aborted` + `Canceled` | **没有** | **409** ✘ |

★ **判据是「设备侧有没有一条 Completed（成功扫完过）」**：
  - 「有没有非 Completed 的记录」**不成立**（四台全是 True，`.101` 却能扫）；
  - 「最近一条是不是 Completed」**也不成立**（`.101` 最新那条是我探测时留下的
    Aborted，它照样能扫）；
  - **只有「有没有 Completed」能一致区分四台。**
★ 409 的响应体**全空**、而 `pwg:State` 仍报 `Idle` ⇒ 用户只看到"打印机坏了"；
  ⇒ 文案必须说清是「**上次没正常扫完**」并给出**立刻能做的动作**。
★ 设备侧残留 **DELETE 只会得到 404/410** ⇒ **客户端清不掉** ⇒ 只能重启打印机。
"""

from unittest import mock

import pytest

import escl_engine


def _status_xml(jobs):
    """造一份 ScannerStatus XML（jobs = [(age, state), ...]）"""
    body = []
    for age, state in jobs:
        body.append(
            "<scan:JobInfo>"
            "<pwg:JobUri>/eSCL/ScanJobs/x%d</pwg:JobUri>"
            "<scan:Age>%d</scan:Age>"
            "<pwg:ImagesCompleted>0</pwg:ImagesCompleted>"
            "<pwg:ImagesToTransfer>0</pwg:ImagesToTransfer>"
            "<pwg:JobState>%s</pwg:JobState>"
            "<pwg:JobStateReasons><pwg:JobStateReason>JobCanceledByUser"
            "</pwg:JobStateReason></pwg:JobStateReasons>"
            "</scan:JobInfo>" % (age, age, state))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<scan:ScannerStatus '
            'xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" '
            'xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">'
            '<pwg:Version>2.63</pwg:Version><pwg:State>Idle</pwg:State>'
            "<scan:Jobs>%s</scan:Jobs></scan:ScannerStatus>"
            % "".join(body))


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class TestJobStateParsing:
    def test_parses_all_job_states(self):
        xml = _status_xml([(100, "Completed"), (200, "Canceled")])
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, xml)):
            ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert ok is True
        assert set(st["job_states_all"]) == {"Completed", "Canceled"}

    def test_has_completed_job_true(self):
        xml = _status_xml([(100, "Completed"), (200, "Canceled")])
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, xml)):
            _ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert st["has_completed_job"] is True

    def test_has_completed_job_false(self):
        xml = _status_xml([(200, "Canceled"), (300, "Aborted")])
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, xml)):
            _ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert st["has_completed_job"] is False

    def test_newest_job_first(self):
        """★ 最新的作业排前面（age 越大越新）"""
        xml = _status_xml([(100, "Completed"), (300, "Aborted"),
                           (200, "Canceled")])
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, xml)):
            _ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert st["last_job_state"] == "Aborted"

    def test_abnormal_age_sorts_last(self):
        """★ `.203` 的 age 是 4290491337 秒（≈136 年）⇒ 异常值必须排到最后"""
        xml = _status_xml([(4290491337, "Canceled"), (100, "Completed")])
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, xml)):
            _ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert st["last_job_state"] == "Completed"

    def test_no_jobs_leaves_unknown(self):
        xml = _status_xml([])
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, xml)):
            _ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert st["has_completed_job"] is None
        assert st["last_job_state"] == ""


class TestConflictMessagePicksTheRightStory:
    def _msg(self, jobs):
        xml = _status_xml(jobs)
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, xml)):
            return escl_engine._conflict_message_for("http://1.2.3.4/eSCL/")

    def test_stale_device_gets_the_precise_message(self):
        """没有 Completed ⇒ 说清「上次没正常结束」+ 是哪个状态"""
        msg = self._msg([(200, "Canceled"), (300, "Aborted")])
        assert "没有正常结束" in msg, msg
        assert "Canceled" in msg or "Aborted" in msg, msg
        assert "打印机面板" in msg, msg
        assert "断电" in msg, "必须给出断电指引（面板重启可能清不掉）"

    def test_device_with_completed_job_gets_generic_message(self):
        """有 Completed（这台机器其实能扫）⇒ 不要说"上次没扫完"（那是误导）"""
        msg = self._msg([(100, "Completed"), (300, "Aborted")])
        assert "没有正常结束" not in msg, msg
        assert "打印机面板" in msg, msg
        assert "断电" in msg, msg

    def test_no_records_at_all_gets_the_internal_marker_message(self):
        """★ 一条记录都没有 ⇒ 设备内部还留着标记（状态接口查不到）⇒ 必须说断电

        ★ 实测 2026-10-23：17.17.171.208 / .111 的作业记录**已经空了**
        （被重启过、记录已被设备清掉）⇒ **却仍然 409** ⇒ 那个标记在设备内部、
        状态接口查不到 ⇒ 面板上的「重启」键可能清不掉，**必须断电**。
        """
        msg = self._msg([])
        assert "查不到任何作业记录" in msg, msg
        assert "断电" in msg, msg
        assert "最近一次" not in msg, "没有记录时不能说『最近一次』"

    def test_all_three_variants_mention_power_cycle(self):
        """三种措辞**共有**的关键信息：面板 + 断电 10 秒"""
        for jobs in ([(200, "Canceled")], [(100, "Completed")], []):
            msg = self._msg(jobs)
            assert "打印机面板" in msg, msg
            assert "断电" in msg, msg

    def test_message_never_promises_the_device_will_heal_by_itself(self):
        """★ 不得承诺「等 N 分钟它就会自己好」——实测卡住的设备**几天都不恢复**，
        等多久都是白等。

        ★ **2026-10-24 口径细化**（别与"开机要几分钟才就绪"混为一谈）：
          · **禁止**：把"分钟"用在「等一等设备自己会恢复」上（假承诺）；
          · **允许且必须**：写明「**等打印机启动完成（约 5 分钟）**」——
            那是**实测事实**（2026-10-24 `.111`：重启后第 5 分钟才受理新作业）。
            没有这句，用户重启完**马上扫**照样撞 409 ⇒ 会以为这招没用。
        """
        for jobs in ([(200, "Canceled")], [(100, "Completed"), (300, "Aborted")],
                     []):
            msg = self._msg(jobs)
            assert "15-20 分钟" not in msg, msg
            for forbidden in ("等一会儿就好", "等几分钟就好", "会自己恢复",
                               "自行恢复", "很快就会好"):
                assert forbidden not in msg, msg

    def test_all_three_variants_mention_boot_wait(self):
        """★ 三种措辞都必须写明「等打印机启动完成」——否则用户重启完马上扫，
        又会撞 409（2026-10-24 `.111` 实测：重启后第 5 分钟才受理）。"""
        for jobs in ([(200, "Canceled")], [(100, "Completed")], []):
            msg = self._msg(jobs)
            assert "启动" in msg, msg
            assert "分钟" in msg, msg
            assert "再试" in msg, msg

class TestJobCleanupIsNotAbandoned:
    """★ 「中途放弃」是设备留下 Canceled/Aborted 的根源 ⇒ 收尾**绝不能**取消作业

    ★ 2026-10-24 真机实测坐实（台账 §13.32）：
      DELETE 的语义是「**取消**设备上的作业」⇒ 设备把作业记成
      `JobStateReason=JobCanceledByUser` ⇒ **M232 机型上扫描子系统永久锁死**
      （`POST /ScanJobs` 一律 409；客户端再 DELETE 只得 404/410 ⇒ 只有断电能清）。
      而文档一取走，设备**自己**就把作业记成 `Completed` 并回收
      （`.101` 实测：随后发出的 DELETE 拿到的是 404）
      ⇒ DELETE 从头到尾就没帮过我们，只留下过锁死的设备。
    """

    def test_cleanup_never_deletes_the_job(self):
        """★★ 红线：收尾三条路径都**不得**发 DELETE。"""
        import inspect
        for fn in (escl_engine.execute_scan, escl_engine.execute_multipage_scan,
                   escl_engine.cleanup_pending_jobs):
            src = inspect.getsource(fn)
            assert ".delete(" not in src, \
                "★ %s 的收尾里出现了 DELETE ⇒ 会把设备上的作业 cancel 掉 ⇒ " \
                "M232 永久锁死（见台账 §13.32）" % fn.__name__

    def test_cleanup_takes_the_document_instead(self):
        """收尾的唯一动作是「把文档取走」（`_drain_job`）。"""
        import inspect
        for fn in (escl_engine.execute_scan, escl_engine.execute_multipage_scan,
                   escl_engine.cleanup_pending_jobs):
            src = inspect.getsource(fn)
            assert "_drain_job" in src, \
                "%s 的收尾必须先把文档取走" % fn.__name__
