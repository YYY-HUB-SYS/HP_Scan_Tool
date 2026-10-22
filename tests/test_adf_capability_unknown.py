"""
「输稿器能力**未知**」契约测试 — HP Scan Tool v4.0

★ 背景（用户 2026-10-22 实测发现）：`17.17.171.111`（HP LaserJet MFP M232dwc）
  与 `17.17.171.203`（M232）**都有输稿器**，用户当天还在 `.111` 上扫过；
  但我们的能力探测把这两台判成了「**没有输稿器**」⇒
  **GUI 的「来源」下拉里根本不出现「ADF」选项** ⇒ 用户想用输稿器也选不到。

★ 两台误判的**成因不同**，但都源于同一个错：**把「没查到」当成了「没有」**
  | 设备 | 能力文档 | ScannerStatus | 我们的判定 | 实际 |
  |---|---|---|---|---|
  | `.101` 7720 | 有 `<scan:Adf>` 段 | 有 AdfState | 有 ADF ✔ | 有 |
  | `.208` M232 | **http+https 都拿不到** | 有 AdfState | 有 ADF ✔ | 有 |
  | `.203` M232 | **http+https 都拿不到** | **没有 AdfState** | **没有 ✘** | **有** |
  | `.111` M232dwc | **文档里没有 Adf 段** | **没有 AdfState** | **没有 ✘** | **有** |

★ 新契约（三态）：
  ① `has_adf=True` 且 `adf_capability_known=True` ⇒ 设备**明确报了**有输稿器；
  ② `has_adf=False` 且 `adf_capability_known=True` ⇒ 设备**明确报了**没有；
  ③ `has_adf=False` 且 `adf_capability_known=False` ⇒ **不知道**（≠ 没有）
     ⇒ **保留用户显式的「输稿器」选择**，GUI **仍然显示「ADF」选项**，
        由设备在建 job 时裁决（没纸就是 503，我们已有精确文案）。
"""

import ast
import io
import os
from unittest import mock

import pytest

import escl_engine
import scan_coordinator


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CAPS_WITH_ADF = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
 xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
 <pwg:MakeAndModel>HP LaserJet MFP M232dwc</pwg:MakeAndModel>
 <scan:Platen><pwg:MaxWidth>2550</pwg:MaxWidth><pwg:MaxHeight>3508</pwg:MaxHeight></scan:Platen>
 <scan:Adf><scan:AdfSimplexInputCaps><pwg:MaxWidth>2550</pwg:MaxWidth></scan:AdfSimplexInputCaps>
  <scan:FeederCapacity>35</scan:FeederCapacity></scan:Adf>
</scan:ScannerCapabilities>"""

CAPS_NO_ADF = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
 xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
 <pwg:MakeAndModel>HP LaserJet MFP M232dwc</pwg:MakeAndModel>
 <scan:Platen><pwg:MaxWidth>2550</pwg:MaxWidth><pwg:MaxHeight>3508</pwg:MaxHeight></scan:Platen>
</scan:ScannerCapabilities>"""

STATUS_WITH_ADF = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
 xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
 <pwg:State>Idle</pwg:State><scan:AdfState>ScannerAdfEmpty</scan:AdfState>
</scan:ScannerStatus>"""


class _Resp:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _scanner():
    s = escl_engine.ScannerInfo(name="m232", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    return s


class TestAdfCapabilityTriState:
    def test_adf_section_reported_means_known_and_present(self):
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(200, CAPS_WITH_ADF)):
            s = escl_engine.fetch_capabilities(_scanner(), timeout=3.0)
        assert s.has_adf is True
        assert s.adf_capability_known is True

    def test_no_adf_section_means_unknown_not_absent(self):
        """★ 核心：文档里没有 Adf 段 ⇒ 「未知」，**不是**「设备没有」"""
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(200, CAPS_NO_ADF)):
            s = escl_engine.fetch_capabilities(_scanner(), timeout=3.0)
        assert s.has_adf is False
        assert s.adf_capability_known is False, (
            "文档里没有 Adf 段 ≠ 设备没有输稿器（实测 .111 明明有）")

    def test_adf_state_in_status_means_known_and_present(self):
        """能力文档拿不到、但 ScannerStatus 报了 AdfState ⇒ 知道有"""
        s = _scanner()
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(500, "")):
            with mock.patch("escl_engine._probe_adf_from_status") as probe:
                def _fake(scanner, base, timeout):
                    scanner.has_adf = True
                    scanner.adf_capability_known = True
                probe.side_effect = _fake
                escl_engine.fetch_capabilities(s, timeout=3.0)
        assert s.has_adf is True
        assert s.adf_capability_known is True

    def test_status_probe_sets_known_flag(self):
        """`_probe_adf_from_status` 找到 AdfState ⇒ 必须置位「能力已知」"""
        s = _scanner()
        with mock.patch("escl_engine.requests.get",
                        return_value=_Resp(200, STATUS_WITH_ADF)):
            escl_engine._probe_adf_from_status(s, "http://1.2.3.4/eSCL/", 3.0)
        assert s.has_adf is True
        assert s.adf_capability_known is True


class TestSourceDecisionWithUnknownAdf:
    def _coord(self):
        return scan_coordinator.ScanCoordinator()

    def test_unknown_adf_keeps_user_choice(self):
        """★ 未知 + 用户选输稿器 ⇒ 保留输稿器（不静默改平板）"""
        coord = self._coord()
        s = _scanner()
        s.has_adf = False
        s.adf_capability_known = False
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle",
                                             "adf_loaded": False})):
            source, adf, warning = coord.resolve_source_detailed(s, "Feeder")
        assert source == "Feeder"
        assert warning == scan_coordinator.WARN_CAPABILITY_QUERY_FAILED

    def test_reported_no_adf_forces_platen(self):
        """设备**明确报告**没有输稿器 ⇒ 强制平板"""
        coord = self._coord()
        s = _scanner()
        s.has_adf = False
        s.adf_capability_known = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle",
                                             "adf_loaded": False})):
            source, adf, warning = coord.resolve_source_detailed(s, "Feeder")
        assert source == "Platen"
        assert warning == scan_coordinator.WARN_NO_ADF


class TestGuiStillOffersAdfWhenUnknown:
    """★ GUI 侧：能力未知时**仍然显示「ADF」选项**（否则用户根本选不到）"""

    def _gui_source(self):
        with io.open(os.path.join(REPO_ROOT, "hp_scan_gui_pyside.py"),
                     "r", encoding="utf-8") as fh:
            return fh.read()

    def test_update_source_options_accepts_unknown_flag(self):
        src = self._gui_source()
        assert "adf_unknown" in src, "GUI 没有 adf_unknown 参数"

    def test_adf_option_shown_when_unknown(self):
        src = self._gui_source()
        assert "if has_adf or adf_unknown:" in src, (
            "能力未知时没有显示「ADF」选项 ⇒ 用户选不到输稿器")

    def test_call_site_passes_unknown_flag(self):
        src = self._gui_source()
        assert "_adf_unknown" in src, "调用点没有把「未知」传进去"
