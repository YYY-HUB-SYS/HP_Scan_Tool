"""
escl_engine 协议层健壮性测试 — HP Scan Tool v4.0
覆盖: 机型标签 MakeAndModel 解析 / ScanJob 409 冲突退避重试 / 301→HTTPS 宽松 TLS 兜底 / 状态探测成败显式区分
"""

from unittest import mock

import pytest
import requests

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

# 两个机型标签同时存在（Model 在前、MakeAndModel 在后）
BOTH_TAGS_CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                          xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:Model>HP Generic Model</pwg:Model>
  <pwg:MakeAndModel>HP LaserJet MFP M232dwc</pwg:MakeAndModel>
  <pwg:SerialNumber>VNP6N40672</pwg:SerialNumber>
  <scan:Platen>
    <pwg:MaxWidth>2550</pwg:MaxWidth>
    <pwg:MaxHeight>3508</pwg:MaxHeight>
  </scan:Platen>
</scan:ScannerCapabilities>"""

# 两个机型标签同时存在，顺序反过来（MakeAndModel 在前、Model 在后）
BOTH_TAGS_REVERSED_CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                          xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:MakeAndModel>HP LaserJet MFP M232dwc</pwg:MakeAndModel>
  <pwg:Model>HP Generic Model</pwg:Model>
  <pwg:SerialNumber>VNP6N40672</pwg:SerialNumber>
  <scan:Platen>
    <pwg:MaxWidth>2550</pwg:MaxWidth>
    <pwg:MaxHeight>3508</pwg:MaxHeight>
  </scan:Platen>
</scan:ScannerCapabilities>"""

STATUS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:State>Idle</pwg:State>
  <scan:AdfState>ScannerAdfEmpty</scan:AdfState>
</scan:ScannerStatus>"""

UNKNOWN_STATE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:State>Unknown</pwg:State>
</scan:ScannerStatus>"""


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

    def test_make_and_model_wins_over_model(self):
        """两个标签同时存在时，MakeAndModel 优先"""
        s = _scanner()
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(200, BOTH_TAGS_CAPS_XML)):
            s = fetch_capabilities(s, timeout=3.0)
        assert s.model == "HP LaserJet MFP M232dwc"

    def test_make_and_model_wins_regardless_of_tag_order(self):
        """标签顺序反过来时也必须 MakeAndModel 优先（守住 model_tag is None 守卫）"""
        s = _scanner()
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(200, BOTH_TAGS_REVERSED_CAPS_XML)):
            s = fetch_capabilities(s, timeout=3.0)
        assert s.model == "HP LaserJet MFP M232dwc"


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
        # 退避入参必须逐个锁定（2 秒起步，逐次翻倍）
        assert [c.args[0] for c in sleeper.call_args_list] == [2, 4]

    def test_persistent_conflict_raises_actionable_message(self):
        s = _scanner()
        session = mock.MagicMock()
        session.post.return_value = _Resp(409, "busy")
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(RuntimeError) as exc:
                escl_engine._create_scan_job(
                    session, s, 300, "RGB24", "jpg", "Platen", False)
        msg = str(exc.value)
        assert "409" in msg
        assert "未完成" in msg
        assert "打印机面板" in msg
        assert session.post.call_count == 4
        # 3 次退避的完整入参序列必须是 2/4/8 秒
        assert [c.args[0] for c in sleeper.call_args_list] == [2, 4, 8]

    def test_non_conflict_error_keeps_original_message(self):
        s = _scanner()
        session = mock.MagicMock()
        session.post.return_value = _Resp(500, "boom")
        with pytest.raises(RuntimeError) as exc:
            escl_engine._create_scan_job(
                session, s, 300, "RGB24", "jpg", "Platen", False)
        assert "500" in str(exc.value)
        # 原句前缀不得被改动
        assert str(exc.value).startswith("创建扫描任务失败 HTTP 500")
        assert session.post.call_count == 1

    def test_service_unavailable_raises_actionable_message(self):
        """输稿器空载时打印机在创建任务阶段返回 503，错误信息必须可执行。

        ★ 2026-10-21 契约变更：503 的处置**按来源分流** ——
        输稿器空载时"等多久都不会成功"（除非用户中途放纸，而 UI 不会提示）
        ⇒ **不重试**，第一次就抛可执行错误（文案含放纸指引）。
        （平板的 503 走有界退避重试，见 tests/test_escl_error_messages.py。）
        """
        s = _scanner()
        session = mock.MagicMock()
        session.post.return_value = _Resp(503, "")
        with mock.patch("escl_engine.time.sleep") as sleeper:
            with pytest.raises(RuntimeError) as exc:
                escl_engine._create_scan_job(
                    session, s, 300, "RGB24", "jpg", "Feeder", False)
        msg = str(exc.value)
        assert "503" in msg
        assert "输稿器" in msg
        # 必须给出"放纸"这个可执行动作，不能只说设备名
        assert "纸张" in msg
        assert "放纸后请重试" in msg, f"必须明确告诉用户放纸后重试: {msg}"
        # 输稿器 503 **不重试**：只发一次请求，也不等待
        assert session.post.call_count == 1, "输稿器 503 不得重试"
        assert not sleeper.called, "输稿器 503 不得退避等待"

    def test_conflict_then_other_error_keeps_original_message(self):
        s = _scanner()
        session = mock.MagicMock()
        session.post.side_effect = [_Resp(409, "busy"), _Resp(500, "boom")]
        with mock.patch("escl_engine.time.sleep"):
            with pytest.raises(RuntimeError) as exc:
                escl_engine._create_scan_job(
                    session, s, 300, "RGB24", "jpg", "Platen", False)
        assert "500" in str(exc.value)
        assert "未完成" not in str(exc.value)


class TestLooseTlsFallback:
    """301 跳 HTTPS 的老机型：TLS cipher 不兼容时的兜底路径"""

    def test_http_redirect_to_https_uses_loose_tls(self):
        s = _scanner()
        s.escl_url = "http://1.2.3.4:80/eSCL/"
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(301, "",
                                           {"Location": "https://1.2.3.4/eSCL/ScannerCapabilities"})), \
             mock.patch("escl_engine._http_get_loose_tls",
                        return_value=(200, CAPS_XML)) as loose:
            s = fetch_capabilities(s, timeout=3.0)
        assert loose.called, "301 指向 https 时应走宽松 TLS 备用路径"
        assert s.model == "HP LaserJet MFP M232dwc"
        assert s.max_width == 2550

    def test_https_direct_failure_falls_back_to_loose_tls(self):
        s = _scanner()
        s.escl_url = "https://1.2.3.4/eSCL/"
        with mock.patch("escl_engine.requests.get",
                        side_effect=requests.exceptions.SSLError("handshake failure")), \
             mock.patch("escl_engine._http_get_loose_tls",
                        return_value=(200, CAPS_XML)) as loose:
            s = fetch_capabilities(s, timeout=3.0)
        assert loose.called
        assert s.model == "HP LaserJet MFP M232dwc"

    def test_loose_tls_failure_is_silent(self):
        """宽松 TLS 也失败时静默回退，不抛异常、不影响扫描"""
        s = _scanner()
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(301, "",
                                           {"Location": "https://1.2.3.4/eSCL/ScannerCapabilities"})), \
             mock.patch("escl_engine._http_get_loose_tls",
                        side_effect=Exception("boom")):
            s = fetch_capabilities(s, timeout=3.0)
        assert s.model == ""

    def test_http_redirect_to_http_is_followed(self):
        """301 指向 http:// 路径时直接跟随，不走宽松 TLS"""
        s = _scanner()
        s.escl_url = "http://1.2.3.4:80/eSCL/"
        caps_url = "http://1.2.3.4:80/eSCL/ScannerCapabilities"
        with mock.patch("escl_engine.requests.get",
                        side_effect=[_Resp(301, "", {"Location": caps_url}),
                                     _Resp(200, CAPS_XML)]) as getter, \
             mock.patch("escl_engine._http_get_loose_tls") as loose:
            s = fetch_capabilities(s, timeout=3.0)
        assert getter.call_count == 2
        assert not loose.called
        assert s.model == "HP LaserJet MFP M232dwc"


class TestProbeScannerStatus:
    """探测成败必须显式可区分，不能靠状态字符串推断"""

    def test_probe_success_returns_ok_true(self):
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, STATUS_XML)):
            ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert ok is True
        assert st["state"] == "Idle"
        assert st["adf_loaded"] is False

    def test_probe_failure_returns_ok_false(self):
        with mock.patch("escl_engine.requests.get", return_value=_Resp(500, "")):
            ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert ok is False
        assert st["state"] == "Unknown"

    def test_legit_unknown_state_is_not_a_failure(self):
        """设备合法上报 <State>Unknown</State> 时，探测仍算成功"""
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, UNKNOWN_STATE_XML)):
            ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert ok is True
        assert st["state"] == "Unknown"

    def test_probe_malformed_xml_returns_ok_false(self):
        """XML 畸形属探测失败，必须显式返回 ok=False"""
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, "<not-xml")):
            ok, st = escl_engine.probe_scanner_status("http://1.2.3.4/eSCL/")
        assert ok is False
        assert st["state"] == "Unknown"

    def test_get_scanner_status_contract_unchanged(self):
        """get_scanner_status 必须保持**原有键**（cli.py cmd_status 依赖它）

        ★ 2026-10-22：新增 `adf_state`（AdfState 的**原值**）——
          公网实现（NAPS2 / sane-airscan / node-hp-scan-to）都把 AdfState
          细分到 Empty / Jam / HatchOpen / Mispick …，我们只做
          `"Loaded" in text` 的布尔化 ⇒ 卡纸与没纸分不开，给不出对症指引。
        ★ **cli.py 不受影响**：它用的是 `if "adf_loaded" in status` 这种
          **存在性**判断，不是精确键集合 ⇒ 多一个键不会破坏它。
        """
        with mock.patch("escl_engine.requests.get", return_value=_Resp(200, STATUS_XML)):
            st = escl_engine.get_scanner_status("http://1.2.3.4/eSCL/")
        assert {"state", "adf_loaded"} <= set(st.keys())
        assert st["state"] == "Idle"
        assert st["adf_loaded"] is False
        # ★ AdfState 原值（2026-10-22 新增）：能区分「没纸」与「卡纸」
        assert st["adf_state"] == "ScannerAdfEmpty"