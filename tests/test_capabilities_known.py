"""
capabilities_known 终局方案 + probe_escl port 形参 + wsd 去重合并 — HP Scan Tool v4.0

对应独立核销报告（verify-v1 §5 N-1 / verify-v0 §5 N-2、N-8）的三条问题：

  ① 能力"已知"信号不可靠：原实现用"能力字段指纹前后对比"当主判据，
     在真实链路上有 3 条失效路径（2 条系统性）——
       ① WSD 发现链路（wsd_engine 先查过一次 ⇒ 第二次是幂等 no-op）
       ② GUI 探测链路（GUI 直接调 fetch_capabilities）
       ③ 能力字段恰好等于 ScannerInfo 默认值
  ② `probe_escl` 的 `port` 形参被完全忽略（非标端口探不到 + 重复探测）
  ③ `wsd_engine` 按 IP 去重"先到者胜"⇒ 可能丢掉已探活的 eSCL URL

本文件用**真实本地 HTTP server**（不是 mock requests 内部）验证能力查询链路。
"""

import contextlib
from unittest import mock
from urllib.parse import urlparse

import pytest
import requests

import escl_engine
import scan_coordinator
import wsd_engine
from escl_engine import ScannerInfo

from fake_http_server import fake_http_server


# ── 假的 eSCL 设备（真 HTTP server，监听 127.0.0.1 的随机高位端口）────────

CAPS_NO_ADF_XML = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                          xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:MakeAndModel>HP Fake Flatbed</pwg:MakeAndModel>
  <pwg:SerialNumber>TEST0001</pwg:SerialNumber>
  <scan:Platen>
    <pwg:MaxWidth>2550</pwg:MaxWidth>
    <pwg:MaxHeight>3508</pwg:MaxHeight>
  </scan:Platen>
</scan:ScannerCapabilities>"""

# 能力字段**恰好等于 ScannerInfo 默认值**的那一组（max_width=2480 /
# max_height=3508 / 无分辨率·颜色·格式·型号·序列号）⇒ 指纹前后完全相同。
# 旧启发式会把这次**成功**的查询误判为"能力未知"。
CAPS_DEFAULTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                          xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <scan:Platen>
    <pwg:MaxWidth>2480</pwg:MaxWidth>
    <pwg:MaxHeight>3508</pwg:MaxHeight>
  </scan:Platen>
</scan:ScannerCapabilities>"""

MALFORMED_XML = "<not-valid-xml"


@contextlib.contextmanager
def _fake_escl_server(body=CAPS_NO_ADF_XML, status=200, delay=0.0):
    """起一个真的本地 HTTP server；返回 base url（形如 http://127.0.0.1:54321）

    ★ 走 `tests/fake_http_server.py` 的**加固**实现（端口独占 + 服务端 I/O
    超时 + 吞掉客户端断开异常 + teardown 不 join 卡死线程）。

    原先直接用 `http.server.ThreadingHTTPServer` 时，本文件在**全量上下文**里
    会**必现**地把整个 pytest 进程干掉：Windows 上 `SO_REUSEADDR` 允许两个
    假 server 绑同一端口 ⇒ 连接被投递到其中一个 ⇒ 客户端等 status line、
    另一个 server 的 handler 卡在 sendall ⇒ 双向阻塞 ⇒ 进程 abort / 挂死。
    根因与实测数据见 `tests/fake_http_server.py` 顶部说明。
    """
    with fake_http_server(body, status=status, delay=delay) as base:
        yield base


@pytest.fixture
def coordinator():
    c = scan_coordinator.ScanCoordinator()
    c.cfg = {}
    return c


def _scanner_with(base: str) -> ScannerInfo:
    s = ScannerInfo(name="fake", ip="127.0.0.1")
    s.escl_url = base.rstrip("/") + "/eSCL/"
    s.has_adf = False
    return s


# ── ① capabilities_known 信号 ────────────────────────────────────────────


class TestCapabilitiesKnownSignal:
    def test_default_is_unknown(self):
        assert ScannerInfo(name="t", ip="1.2.3.4").capabilities_known is False

    def test_successful_query_marks_known(self):
        with _fake_escl_server(CAPS_NO_ADF_XML) as base:
            s = _scanner_with(base)
            escl_engine.fetch_capabilities(s, timeout=3.0)
        assert s.capabilities_known is True
        assert s.has_adf is False
        assert s.model == "HP Fake Flatbed"

    def test_fields_equal_to_defaults_still_known(self):
        """失效路径 ③ 的护栏：能力字段恰好等于默认值 ⇒ 成功查询仍判"已知"。

        旧启发式（指纹前后完全相同 ⇒ 未知）在这里会误判。
        """
        with _fake_escl_server(CAPS_DEFAULTS_XML) as base:
            s = _scanner_with(base)
            before = scan_coordinator._capability_fingerprint(s)
            escl_engine.fetch_capabilities(s, timeout=3.0)
            after = scan_coordinator._capability_fingerprint(s)
        assert before == after, "本用例的前提：指纹前后完全相同"
        assert s.capabilities_known is True

    def test_connection_failure_keeps_unknown(self):
        """查询失败（连不上）⇒ capabilities_known 保持 False。"""
        # 监听一个随即关闭的端口 ⇒ 连接被拒
        with _fake_escl_server() as base:
            pass
        s = _scanner_with(base)
        escl_engine.fetch_capabilities(s, timeout=1.0)
        assert s.capabilities_known is False

    def test_timeout_keeps_unknown(self):
        """查询超时 ⇒ capabilities_known 保持 False。"""
        s = ScannerInfo(name="fake", ip="127.0.0.1")
        s.escl_url = "http://127.0.0.1:1/eSCL/"
        with mock.patch("escl_engine.requests.get",
                        side_effect=requests.exceptions.ReadTimeout("timed out")):
            escl_engine.fetch_capabilities(s, timeout=0.1)
        assert s.capabilities_known is False

    def test_malformed_xml_keeps_unknown(self):
        """HTTP 200 但 XML 畸形 ⇒ capabilities_known 保持 False。"""
        with _fake_escl_server(MALFORMED_XML) as base:
            s = _scanner_with(base)
            escl_engine.fetch_capabilities(s, timeout=3.0)
        assert s.capabilities_known is False

    def test_non_200_keeps_unknown(self):
        """非 200（走 ScannerStatus 回退探测）⇒ 不是能力文档 ⇒ 保持 False。"""
        with _fake_escl_server("boom", status=500) as base:
            s = _scanner_with(base)
            escl_engine.fetch_capabilities(s, timeout=3.0)
        assert s.capabilities_known is False

    def test_no_escl_url_keeps_unknown(self):
        s = ScannerInfo(name="fake", ip="1.2.3.4")
        escl_engine.fetch_capabilities(s, timeout=1.0)
        assert s.capabilities_known is False

    def test_known_is_not_cleared_by_later_failure(self):
        """已确认的能力不会被后续一次失败查询抹掉（否则又造出"假未知"）。"""
        with _fake_escl_server(CAPS_NO_ADF_XML) as base:
            s = _scanner_with(base)
            escl_engine.fetch_capabilities(s, timeout=3.0)
            assert s.capabilities_known is True
            with mock.patch("escl_engine.requests.get",
                            side_effect=requests.exceptions.ReadTimeout("timed out")):
                escl_engine.fetch_capabilities(s, timeout=0.1)
        assert s.capabilities_known is True


class TestKnownSignalOnRealLinkages:
    """失效路径 ①（WSD 链路）与 ②（GUI 链路）的回归护栏。"""

    def test_wsd_linkage_fetch_then_tracked_stays_known(self):
        """WSD 链路：先 fetch_capabilities（wsd_engine 干的事），再
        fetch_capabilities_tracked（协调器干的事）⇒ 第二次**仍判"已知"**。

        旧实现下第二次是幂等 no-op ⇒ 指纹不变 ⇒ 永远判"能力未知"。
        """
        with _fake_escl_server(CAPS_NO_ADF_XML) as base:
            s = _scanner_with(base)
            # ① wsd_engine._build_scanner_from_wsd 里的那一调
            escl_engine.fetch_capabilities(s, timeout=3.0)
            assert s.capabilities_known is True
            # ② scan_coordinator 里的那一调
            scan_coordinator.fetch_capabilities_tracked(s, timeout=3.0)
        assert s.capabilities_known is True
        assert scan_coordinator.capabilities_known(s) is True

    def test_gui_linkage_direct_fetch_is_known(self):
        """GUI 链路：GUI 直接调 fetch_capabilities（不打标）⇒ 现在字段自己就真了。"""
        with _fake_escl_server(CAPS_NO_ADF_XML) as base:
            s = _scanner_with(base)
            escl_engine.fetch_capabilities(s, timeout=3.0)
        assert s.capabilities_known is True

    def test_tracked_fetch_uses_field_not_fingerprint(self):
        """主判据是 capabilities_known 字段，不是指纹对比。"""
        with _fake_escl_server(CAPS_DEFAULTS_XML) as base:
            s = _scanner_with(base)
            scan_coordinator.fetch_capabilities_tracked(s, timeout=3.0)
        assert scan_coordinator.capabilities_known(s) is True

    def test_tracked_fetch_fingerprint_fallback_still_works(self):
        """兜底保留：调用方（被 mock 的 fetch_capabilities）没置位、但字段
        确实变了 ⇒ 仍补判为"已知"。"""
        s = ScannerInfo(name="t", ip="1.2.3.4")

        def _fake_fetch(scanner, timeout=3.0):
            scanner.model = "HP M232"
            return scanner

        with mock.patch("scan_coordinator.fetch_capabilities", _fake_fetch):
            scan_coordinator.fetch_capabilities_tracked(s, timeout=1.0)
        assert scan_coordinator.capabilities_known(s) is True

    def test_tracked_fetch_failure_keeps_unknown(self):
        s = ScannerInfo(name="t", ip="1.2.3.4")
        with mock.patch("scan_coordinator.fetch_capabilities",
                        lambda scanner, timeout=3.0: scanner):
            scan_coordinator.fetch_capabilities_tracked(s, timeout=1.0)
        assert scan_coordinator.capabilities_known(s) is False


# ── ① 来源决策：能力已知 vs 能力未知 ─────────────────────────────────────


class TestSourceDecisionWithKnownCapability:
    def test_device_without_adf_section_is_unknown_not_no_adf(self, coordinator):
        """★ 2026-10-22：能力文档拿到了、但**没有 `<scan:Adf>` 段**

        ⇒ 只能判「输稿器能力**未知**」，**不能**判「设备没有输稿器」
        （实测 17.17.171.111 / .203 都是 M232 系列、**都有**输稿器，
        一个文档里没这一段、另一个文档根本拿不到）
        ⇒ 保留用户显式的「输稿器」选择，不静默改成平板。
        """
        with _fake_escl_server(CAPS_NO_ADF_XML) as base:
            s = _scanner_with(base)
            escl_engine.fetch_capabilities(s, timeout=3.0)
            source, adf, warning = coordinator.resolve_source_detailed(s, "Feeder")
        assert s.capabilities_known is True
        assert s.adf_capability_known is False, (
            '能力文档里没有 Adf 段 ⇒ 只能是「未知」，不能是「明确没有」')
        assert (source, adf) == ("Feeder", False)
        assert warning == scan_coordinator.WARN_CAPABILITY_QUERY_FAILED

    def test_query_failure_keeps_feeder_with_warning(self, coordinator):
        """能力查询失败 ⇒ capabilities_known=False ⇒ 按用户选择走 + 警告。"""
        with _fake_escl_server() as base:
            pass  # 端口随即关闭 ⇒ 连不上
        s = _scanner_with(base)
        escl_engine.fetch_capabilities(s, timeout=1.0)
        source, adf, warning = coordinator.resolve_source_detailed(s, "Feeder")
        assert s.capabilities_known is False
        assert (source, adf) == ("Feeder", False)
        assert warning == scan_coordinator.WARN_CAPABILITY_QUERY_FAILED

    def test_query_timeout_keeps_feeder_with_warning(self, coordinator):
        s = ScannerInfo(name="fake", ip="127.0.0.1")
        s.escl_url = "http://127.0.0.1:1/eSCL/"
        with mock.patch("escl_engine.requests.get",
                        side_effect=requests.exceptions.ReadTimeout("timed out")):
            escl_engine.fetch_capabilities(s, timeout=0.1)
        source, adf, warning = coordinator.resolve_source_detailed(s, "Feeder")
        assert s.capabilities_known is False
        assert (source, adf) == ("Feeder", False)
        assert warning == scan_coordinator.WARN_CAPABILITY_QUERY_FAILED

    def test_warning_texts_are_distinct(self):
        """两类提示必须是**不同的文案**（查询失败 vs 查询成功但没有 ADF）。"""
        assert scan_coordinator.WARN_CAPABILITY_QUERY_FAILED != scan_coordinator.WARN_NO_ADF
        assert scan_coordinator.WARN_CAPABILITY_UNKNOWN == \
            scan_coordinator.WARN_CAPABILITY_QUERY_FAILED  # 向后兼容别名
        assert "失败" in scan_coordinator.WARN_CAPABILITY_QUERY_FAILED
        assert "不支持输稿器" in scan_coordinator.WARN_NO_ADF

    def test_four_source_branches_not_regressed(self, coordinator):
        """既有四条来源分支不得被改坏（verifier 实测这四条当前都是 ✔）。"""
        s = ScannerInfo(name="t", ip="1.2.3.4")
        s.escl_url = "http://1.2.3.4:80/eSCL/"

        # ① 显式平板：不探测、不覆盖
        s.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status") as probe:
            assert coordinator.resolve_source(s, "Platen") == ("Platen", False)
        assert probe.call_count == 0

        # ② 输稿器有纸 → Feeder
        s.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})):
            assert coordinator.resolve_source(s, "Feeder") == ("Feeder", True)

        # ③ 输稿器无纸 → **保留输稿器**（2026-10-22 起不再静默改成平板）（带 WARN_NO_PAPER）
        s.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": False})):
            source, adf, warning = coordinator.resolve_source_detailed(s, "Feeder")
        assert (source, adf) == ("Feeder", False)
        assert warning == scan_coordinator.WARN_NO_PAPER

        # ④ 自动 → 由探测决定
        s.has_adf = True
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})):
            assert coordinator.resolve_source(s, "Auto") == ("Feeder", True)
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": False})):
            assert coordinator.resolve_source(s, "Auto") == ("Platen", False)


# ── ② probe_escl 的 port 形参 ────────────────────────────────────────────


def _probe_bases(**kwargs):
    """跑一次 probe_escl（把 requests.get 换成"连不上"），返回实际试过的候选 base。

    注意：probe_escl 在 ConnectionError 时会 `break` 掉路径内层循环
    （每个 base 只试 /eSCL/ScannerStatus 一条路径），所以"试过的 base"
    才是要断言的对象（请求条数会随异常类型变化，base 集合才是契约）。
    """
    seen = []

    def _fake_get(url, **kw):
        seen.append(url)
        raise requests.ConnectionError("unreachable")

    with mock.patch("escl_engine.requests.get", _fake_get):
        escl_engine.probe_escl("192.0.2.50", timeout=0.1, **kwargs)
    bases = []
    for u in seen:
        p = urlparse(u)
        b = f"{p.scheme}://{p.netloc}"
        if b not in bases:
            bases.append(b)
    return bases


class TestProbeEsclPort:
    def test_port_is_effective(self):
        """port 真正生效：非 80/443 端口的设备能被探到。"""
        with _fake_escl_server() as base:
            host_port = base.rsplit(":", 1)[1]
            url = escl_engine.probe_escl("127.0.0.1", port=int(host_port), timeout=2.0)
        assert url is not None
        assert f":{host_port}" in url

    def test_default_candidates_unchanged(self):
        """不传 port ⇒ 候选仍是 http:80 + https:443（逐字一致，向后兼容）。"""
        assert _probe_bases() == [
            "http://192.0.2.50:80",
            "https://192.0.2.50:443",
        ]

    def test_port_80_candidates_unchanged(self):
        """显式传 80（GUI 端口框默认值）⇒ 候选与不传时一致（不回归 GUI）。"""
        assert _probe_bases(port=80) == [
            "http://192.0.2.50:80",
            "https://192.0.2.50:443",
        ]

    def test_port_443_candidates_are_different(self):
        """传 443 ⇒ 候选换到 443，**不再**与不传时完全相同（消除重复探测）。"""
        assert _probe_bases(port=443) == [
            "http://192.0.2.50:443",
            "https://192.0.2.50:443",
        ]

    def test_nonstandard_port_candidates(self):
        """非标端口 ⇒ 候选换到该端口（改造前永远探不到）。"""
        assert _probe_bases(port=8080) == [
            "http://192.0.2.50:8080",
            "https://192.0.2.50:8080",
        ]

    def test_default_covers_both_paths_per_base(self):
        """默认候选每个 base 两条路径都试（连接异常之外的异常 ⇒ continue）。"""
        seen = []

        def _fake_get(url, **kw):
            seen.append(url)
            raise RuntimeError("boom")

        with mock.patch("escl_engine.requests.get", _fake_get):
            escl_engine.probe_escl("192.0.2.50", timeout=0.1)
        assert seen == [
            "http://192.0.2.50:80/eSCL/ScannerStatus",
            "http://192.0.2.50:80/ScannerStatus",
            "https://192.0.2.50:443/eSCL/ScannerStatus",
            "https://192.0.2.50:443/ScannerStatus",
        ]

    def test_wsd_engine_does_not_probe_twice(self):
        """wsd_engine 只探一次（那一轮候选已含 https:443）。"""
        calls = []

        def _fake_probe(ip, **kwargs):
            calls.append((ip, kwargs))
            return None

        with mock.patch("wsd_engine.probe_escl", _fake_probe):
            wsd_engine._build_scanner_from_wsd("192.0.2.50", object())
        assert len(calls) == 1
        assert calls[0][0] == "192.0.2.50"
        assert "port" not in calls[0][1], "不再补一轮 port=443（请求 URL 完全重复）"

    def test_cli_style_two_calls_are_not_identical(self):
        """cli.py 的两轮（不传 port / 传 port=443）不再是同一组 URL。

        cli.py 不在本轮文件所有权里，改不了调用点；这里锁住"两轮候选不同"
        这条性质（原先两轮完全相同 ⇒ 每台不可达设备多一轮重复请求）。
        """
        first = set(_probe_bases())
        second = set(_probe_bases(port=443))
        assert first != second
        # 残余重叠只有 https:443 这一个 base（原先是两轮完全相同）
        assert first & second == {"https://192.0.2.50:443"}


# ── ③ wsd_engine 按 IP 去重：合并而不是先到者胜 ───────────────────────────


class TestWsdMergeByIP:
    def test_merge_keeps_escl_url_when_mdns_finishes_first(self):
        """mDNS 先完成（escl_url 为空）、eSCL 后完成（有值）⇒ 最终不丢。"""
        mdns = ScannerInfo(name="mdns-name", ip="192.0.2.7", model="HP Fake Flatbed")
        mdns.escl_url = ""           # mDNS 侧不构造 escl_url
        wsd = ScannerInfo(name="wsd-name", ip="192.0.2.7")
        wsd.escl_url = "http://192.0.2.7/eSCL/"

        merged = wsd_engine._merge_by_ip([mdns, wsd])
        assert len(merged) == 1
        assert merged[0].escl_url == "http://192.0.2.7/eSCL/"
        assert merged[0].model == "HP Fake Flatbed"   # mDNS 侧的型号也保住

    def test_merge_keeps_escl_url_when_wsd_finishes_first(self):
        wsd = ScannerInfo(name="wsd-name", ip="192.0.2.7")
        wsd.escl_url = "http://192.0.2.7/eSCL/"
        mdns = ScannerInfo(name="mdns-name", ip="192.0.2.7", model="HP Fake Flatbed")

        merged = wsd_engine._merge_by_ip([wsd, mdns])
        assert len(merged) == 1
        assert merged[0].escl_url == "http://192.0.2.7/eSCL/"
        assert merged[0].model == "HP Fake Flatbed"

    def test_merge_keeps_one_entry_per_ip(self):
        a = ScannerInfo(name="a", ip="192.0.2.7")
        b = ScannerInfo(name="b", ip="192.0.2.7")
        c = ScannerInfo(name="c", ip="192.0.2.8")
        merged = wsd_engine._merge_by_ip([a, b, c])
        assert [m.ip for m in merged] == ["192.0.2.7", "192.0.2.8"]

    def test_merge_unions_capability_flags(self):
        a = ScannerInfo(name="a", ip="192.0.2.7")
        b = ScannerInfo(name="b", ip="192.0.2.7")
        b.has_adf = True
        b.capabilities_known = True
        merged = wsd_engine._merge_by_ip([a, b])
        assert merged[0].has_adf is True
        assert merged[0].capabilities_known is True

    def test_discover_all_scanners_merges_mdns_and_wsd(self):
        """端到端：mDNS 与 WSD 同时发现同一台 ⇒ 结果只有一条且带 escl_url。"""
        mdns = ScannerInfo(name="mdns-name", ip="192.0.2.7", model="HP Fake Flatbed")
        wsd = ScannerInfo(name="wsd-name", ip="192.0.2.7")
        wsd.escl_url = "http://192.0.2.7/eSCL/"

        with mock.patch("escl_engine.discover_scanners", return_value=[mdns]), \
             mock.patch("wsd_engine.discover_wsd", return_value=[wsd]):
            result = wsd_engine.discover_all_scanners(timeout=0.1)
        assert len(result) == 1
        assert result[0].escl_url == "http://192.0.2.7/eSCL/"
