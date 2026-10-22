"""
listen_engine 清理回归测试 — HP Scan Tool v4.0

锁死 I-2（Important，既有缺陷但与本轮 409 改动耦合）：
  监听模式取消/超时后，finally 只 session.close()，**不删打印机上的 ScanJob**
  ⇒ 打印机上残留一个未完成 job ⇒ 紧接着的普通扫描撞 409
     （提示"打印机上有一个未完成的扫描任务"，其实是自己上次监听留下的），
      且实测可能很久都不被回收。
  修法：监听路径的 finally 同样 session.delete(job_uri)（失败忽略、记 debug），
  与 execute_scan 对齐；job_uri 未绑定（创建就失败）时不能二次抛错。
"""

import threading
from unittest import mock

import pytest

import listen_engine
from escl_engine import ScannerInfo


@pytest.fixture
def scanner():
    s = ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    s.has_adf = True
    return s


JOB_URI = "http://1.2.3.4:80/eSCL/ScanJobs/1"


def _fake_session():
    """假 session：记录 delete 调用，get 永不返回有效文档"""
    session = mock.MagicMock()
    session.verify = True
    response = mock.MagicMock()
    response.status_code = 503          # 扫描进行中 → 一直等
    session.get.return_value = response
    return session


def _patch_session(monkeypatch, session):
    monkeypatch.setattr(listen_engine.requests, "Session", lambda: session)
    return session


def _patch_create_job(monkeypatch, result=None, error=None):
    def fake_create_scan_job(session, scanner, resolution, color_mode,
                             output_format, source, duplex, max_pages=1,
                             cancel_event=None):
        if error is not None:
            raise error
        return ("http://1.2.3.4:80/eSCL", JOB_URI, "jpg")

    monkeypatch.setattr(listen_engine, "_create_scan_job", fake_create_scan_job)


def _cancelled():
    ev = threading.Event()
    ev.set()
    return ev


class TestListenForScanCleansUpJob:
    def test_cancel_deletes_scan_job(self, scanner, monkeypatch):
        """用户点"取消监听" ⇒ 必须删掉打印机上的 ScanJob"""
        session = _patch_session(monkeypatch, _fake_session())
        _patch_create_job(monkeypatch)

        result = listen_engine.listen_for_scan(
            scanner, wait_timeout=30, cancel_event=_cancelled())

        assert result is None
        session.delete.assert_called_once_with(JOB_URI, timeout=3)

    def test_timeout_deletes_scan_job(self, scanner, monkeypatch):
        """等待超时 ⇒ 同样必须删掉 ScanJob"""
        session = _patch_session(monkeypatch, _fake_session())
        _patch_create_job(monkeypatch)

        result = listen_engine.listen_for_scan(scanner, wait_timeout=0)

        assert result is None
        session.delete.assert_called_once_with(JOB_URI, timeout=3)

    def test_job_creation_failure_does_not_raise_secondary_error(self, scanner, monkeypatch):
        """创建 job 就失败时：job_uri 未绑定，不能因为清理逻辑二次抛错"""
        session = _patch_session(monkeypatch, _fake_session())
        _patch_create_job(monkeypatch, error=RuntimeError("打印机不可用"))

        with pytest.raises(RuntimeError) as exc:
            listen_engine.listen_for_scan(scanner, wait_timeout=0)

        assert "打印机不可用" in str(exc.value), "原始错误必须透传"
        assert not session.delete.called, "没有 job_uri 时不该发删除请求"

    def test_delete_failure_is_ignored(self, scanner, monkeypatch):
        """删除失败（打印机不支持 DELETE /ScanJobs）不能影响返回值"""
        session = _patch_session(monkeypatch, _fake_session())
        session.delete.side_effect = RuntimeError("405 Method Not Allowed")
        _patch_create_job(monkeypatch)

        result = listen_engine.listen_for_scan(
            scanner, wait_timeout=0, cancel_event=_cancelled())

        assert result is None, "删除失败不能把监听结果搞崩"


class TestListenForMultipageCleansUpJob:
    def test_cancel_deletes_scan_job(self, scanner, monkeypatch):
        session = _patch_session(monkeypatch, _fake_session())
        _patch_create_job(monkeypatch)

        result = listen_engine.listen_for_multipage_scan(
            scanner, source="Feeder", wait_timeout=30, cancel_event=_cancelled())

        assert result is None
        session.delete.assert_called_once_with(JOB_URI, timeout=3)

    def test_timeout_deletes_scan_job(self, scanner, monkeypatch):
        session = _patch_session(monkeypatch, _fake_session())
        _patch_create_job(monkeypatch)

        result = listen_engine.listen_for_multipage_scan(
            scanner, source="Feeder", wait_timeout=0, page_timeout=0)

        assert result is None
        session.delete.assert_called_once_with(JOB_URI, timeout=3)

    def test_job_creation_failure_does_not_raise_secondary_error(self, scanner, monkeypatch):
        session = _patch_session(monkeypatch, _fake_session())
        _patch_create_job(monkeypatch, error=RuntimeError("打印机不可用"))

        with pytest.raises(RuntimeError) as exc:
            listen_engine.listen_for_multipage_scan(
                scanner, source="Feeder", wait_timeout=0, page_timeout=0)

        assert "打印机不可用" in str(exc.value)
        assert not session.delete.called
