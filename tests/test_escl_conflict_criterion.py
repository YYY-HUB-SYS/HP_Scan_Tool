"""
409 判据契约测试 — HP Scan Tool v4.0

★ 2026-10-27 契约变更：**409 判定不再依赖设备侧可见记录**。

背景（真机实测，见台账 §13.34）：
  · `.111` / `.203` / `.208`（M232）：面板取消键按过、断电重启过，仍 409；
  · `.208` 的设备侧记录已是 `Completed`（旧判据下完全不该挡路），POST 仍 409；
  · 用产品 `_drain_job` 补取残留作业（GET NextDocument）→ 0 字节，仍 409；
  · 代码注释里早有「实测 .208/.111：记录已空，仍 409」。
  ⇒ 设备侧可见记录与 409 **没有因果关系**，设备内部有状态接口看不到的锁定标记。

★ 因此：
  - `_conflict_message_for` **不再探测设备状态、不再按记录分支**，返回统一文案；
  - 统一文案保留可尝试的步骤（面板取消 / 断电重启 / 等启动完成约 5 分钟），
    但明确说明「若仍 409，需恢复默认/出厂设置，或联系管理员」；
  - 文案**不得**承诺设备会自愈。
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
    """probe_scanner_status 的作业解析仍保留（别处还会用），但 409 判定不再依赖它。"""

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
    """★ 2026-10-27 契约：409 文案**统一**，不依赖设备侧可见记录。"""

    def _msg(self, jobs):
        xml = _status_xml(jobs)
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, xml)):
            return escl_engine._conflict_message_for("http://1.2.3.4/eSCL/")

    def test_same_message_regardless_of_device_jobs(self):
        """★★ 无论设备侧记录是 Completed / Canceled / 空，都返回同一份文案。"""
        msgs = {self._msg([(200, "Canceled")]),
                self._msg([(100, "Completed"), (300, "Aborted")]),
                self._msg([])}
        assert len(msgs) == 1, \
            "409 文案不再依赖设备侧可见记录（实测记录与 409 无关），实际: %d 种" \
            % len(msgs)

    def test_message_mentions_the_http_conflict(self):
        msg = self._msg([(200, "Canceled")])
        assert "409" in msg, msg
        assert "未完成" in msg, msg

    def test_message_mentions_ttl_and_client_cannot_clean(self):
        """必须写清「不会很快自行消失」+「客户端清不掉（404/410）」。"""
        msg = self._msg([(200, "Canceled")])
        assert "不会很快自行消失" in msg, msg
        assert "404/410" in msg, msg

    def test_message_gives_tryable_steps(self):
        """保留可尝试的步骤：面板取消 / 断电重启 / 等启动完成。"""
        msg = self._msg([(200, "Canceled")])
        assert "打印机面板" in msg, msg
        assert "断电" in msg, msg
        assert "重启" in msg, msg
        assert "启动" in msg, msg
        assert "分钟" in msg, msg
        assert "再试" in msg, msg

    def test_message_mentions_factory_reset_as_last_resort(self):
        """★★ 必须说明：若上述操作仍 409，需恢复默认/出厂或联系管理员。"""
        msg = self._msg([(200, "Canceled")])
        assert ("恢复默认" in msg) or ("恢复出厂" in msg), msg
        assert "联系" in msg, msg

    def test_message_never_promises_the_device_will_heal_by_itself(self):
        """★ 不得承诺「等 N 分钟它就会自己好」——实测卡住的设备**几天都不恢复**。

        ★ 允许且必须：写明「等打印机启动完成（约 5 分钟）」——
          那是实测事实（重启后第 5 分钟才受理新作业），不是自愈承诺。
        """
        for jobs in ([(200, "Canceled")], [(100, "Completed"), (300, "Aborted")],
                     []):
            msg = self._msg(jobs)
            assert "15-20 分钟" not in msg, msg
            for forbidden in ("等一会儿就好", "等几分钟就好", "会自己恢复",
                               "自行恢复", "很快就会好"):
                assert forbidden not in msg, msg

    def test_does_not_depend_on_device_status_probe(self):
        """★★ 即使设备状态查询失败，409 文案也必须照常给出（不再依赖探测）。"""
        with mock.patch("escl_engine.probe_scanner_status",
                        side_effect=RuntimeError("探测失败")):
            msg = escl_engine._conflict_message_for("http://1.2.3.4/eSCL/")
        assert "409" in msg, msg
        assert "恢复默认" in msg or "恢复出厂" in msg, msg


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