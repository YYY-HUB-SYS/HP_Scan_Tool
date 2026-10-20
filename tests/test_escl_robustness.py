"""
escl_engine 协议层健壮性测试 — HP Scan Tool v4.0
覆盖: 机型标签 MakeAndModel 解析 / ScanJob 409 冲突退避重试
"""

from unittest import mock

import pytest

import escl_engine
from escl_engine import ScannerInfo, fetch_capabilities


CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                          xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:MakeAndModel>HP LaserJet MFP M232dwc</pwg:MakeAndModel>
  <pwg:SerialNumber>VNP6N40672</pwg:SerialNumber>
  <scan:Platen>
    <pwg:MaxWidth>2550</pwg:MaxWidth>
    <pwg:MaxHeight>3508</pwg:MaxHeight>
  </scan:Platen>
</scan:ScannerCapabilities>"""

LEGACY_CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                          xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:Model>HP OfficeJet Pro 7720 series</pwg:Model>
  <pwg:SerialNumber>ABC123</pwg:SerialNumber>
  <scan:Platen>
    <pwg:MaxWidth>2550</pwg:MaxWidth>
    <pwg:MaxHeight>3508</pwg:MaxHeight>
  </scan:Platen>
</scan:ScannerCapabilities>"""


class _Resp:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _scanner():
    s = ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    return s


class TestModelParsing:
    def test_make_and_model_tag_is_parsed(self):
        s = _scanner()
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, CAPS_XML)):
            s = fetch_capabilities(s, timeout=3.0)
        assert s.model == "HP LaserJet MFP M232dwc"
        assert s.serial == "VNP6N40672"
        assert s.max_width == 2550
        assert s.max_height == 3508

    def test_legacy_model_tag_still_parsed(self):
        s = _scanner()
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, LEGACY_CAPS_XML)):
            s = fetch_capabilities(s, timeout=3.0)
        assert s.model == "HP OfficeJet Pro 7720 series"

    def test_display_name_prefers_model(self):
        s = ScannerInfo(name="WSD-Scanner-1.2.3.4", ip="1.2.3.4", model="HP M232")
        assert s.display_name == "HP M232"


class TestScanJobConflict:
    def test_conflict_then_success_after_backoff(self):
        s = _scanner()
        session = mock.MagicMock()
        session.post.side_effect = [
            _Resp(409, "busy"),
            _Resp(409, "busy"),
            _Resp(201, "", {"Location": "http://1.2.3.4:80/eSCL/ScanJobs/7"}),
        ]
        with mock.patch("escl_engine.time.sleep") as sleeper:
            base, job_uri, ext = escl_engine._create_scan_job(
                session, s, 300, "RGB24", "jpg", "Platen", False)
        assert job_uri.endswith("/7")
        assert session.post.call_count == 3
        assert sleeper.call_count == 2

    def test_persistent_conflict_raises_actionable_message(self):
        s = _scanner()
        session = mock.MagicMock()
        session.post.return_value = _Resp(409, "busy")
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(RuntimeError) as exc:
                escl_engine._create_scan_job(
                    session, s, 300, "RGB24", "jpg", "Platen", False)
        msg = str(exc.value)
        assert "409" in msg
        assert "未完成" in msg
        assert "打印机面板" in msg
        assert session.post.call_count == 4

    def test_non_conflict_error_keeps_original_message(self):
        s = _scanner()
        session = mock.MagicMock()
        session.post.return_value = _Resp(500, "boom")
        with pytest.raises(RuntimeError) as exc:
            escl_engine._create_scan_job(
                session, s, 300, "RGB24", "jpg", "Platen", False)
        assert "500" in str(exc.value)
        assert session.post.call_count == 1

    def test_service_unavailable_raises_actionable_message(self):
        """输稿器空载时打印机在创建任务阶段返回 503，错误信息必须可执行"""
        s = _scanner()
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        with pytest.raises(RuntimeError) as exc:
            escl_engine._create_scan_job(
                session, s, 300, "RGB24", "jpg", "Feeder", False)
        msg = str(exc.value)
        assert "503" in msg
        assert "输稿器" in msg
        assert session.post.call_count == 1
