"""
WSD 引擎测试 — HP Scan Tool v4.0
覆盖链号 14.2：`discover_wsd` 的 `except Exception` 兜底**无用例**。

被守护的行为（源码事实，wsd_engine.py:16-61）
------------------------------------------------
    def discover_wsd(timeout: float = 4.0) -> list[ScannerInfo]:
        try:
            from wsdiscovery import WSDiscovery
        except ImportError:
            logger.debug("WSDiscovery 库未安装，跳过 WSD 发现")
            return []                                  # ① 库没装 ⇒ 空列表
        results = []
        wsd = None
        try:
            wsd = WSDiscovery()
            wsd.start()
            services = wsd.searchServices(timeout=timeout)
            for svc in services:
                try:
                    ... 解析 xaddrs → probe_escl → ScannerInfo
                except Exception as e:
                    logger.debug("解析 WSD 服务失败: %s", e)
                    continue                            # ② 单条服务坏 ⇒ 跳过
        except Exception as e:
            logger.debug("WSD 发现失败: %s", e)          # ③ 探测失败 ⇒ 吞掉
        finally:
            if wsd:
                try:
                    wsd.stop()
                except Exception:
                    pass
        return results

⇒ **任何探测阶段的异常都必须收敛成空列表**，不能抛给调用方：
  - `cli.py cmd_discover --wsd-only` 直接调用它（异常 ⇒ CLI 崩）；
  - GUI 的发现线程也走它（异常 ⇒ 线程挂掉）。

★ mock 入口 = `sys.modules["wsdiscovery"]`
  `discover_wsd` 是**函数内导入** `from wsdiscovery import WSDiscovery`
  ⇒ 必须替换**模块对象**（sys.modules 里的那一项），
    给 `wsd_engine` 打属性桩是**拦不住**的。

★ 非空转验证的前提：`_positive_control` 那条用例证明 mock 真的生效
  （假 WSDiscovery 返回一个服务 ⇒ 必须真的产出 1 台 ScannerInfo）。
  没有它，"失败时返回空列表"可能是"mock 根本没被用上"的假绿。

★ 本文件**不发任何真实网络请求**：probe_escl / fetch_capabilities 全部打桩。
"""

import sys
import types

import pytest

import wsd_engine


# ── 假 WSDiscovery ──────────────────────────────────────────

class _FakeService:
    """假的 WS-Discovery 服务对象（只实现 wsd_engine 用到的两个方法）。"""

    def __init__(self, xaddrs=(), display_name=None, raise_on_xaddrs=False):
        self._xaddrs = list(xaddrs)
        self._display_name = display_name
        self._raise_on_xaddrs = raise_on_xaddrs

    def getXAddrs(self):
        if self._raise_on_xaddrs:
            raise RuntimeError("坏掉的 xaddrs")
        return list(self._xaddrs)

    def getDisplayName(self):
        return self._display_name


def _fake_wsd_module(*, services=(), search_error=None, start_error=None,
                     init_error=None, calls=None):
    """
    造一个假的 `wsdiscovery` 模块（放进 sys.modules 即被 `discover_wsd` 用上）。

    calls 记录调用序（init / start / search:<timeout> / stop），
    用来断言 finally 里的清理动作真的执行了。
    """
    calls = calls if calls is not None else []

    class _FakeWSDiscovery:
        def __init__(self, *args, **kwargs):
            calls.append("init")
            if init_error is not None:
                raise init_error

        def start(self):
            calls.append("start")
            if start_error is not None:
                raise start_error

        def searchServices(self, timeout=None):
            calls.append("search:%r" % (timeout,))
            if search_error is not None:
                raise search_error
            return list(services)

        def stop(self):
            calls.append("stop")

    module = types.ModuleType("wsdiscovery")
    module.WSDiscovery = _FakeWSDiscovery
    module.__spec__ = None
    return module


# ── 14.2：探测失败不抛，收敛成空列表 ──────────────────────

class TestDiscoverWsdFailureIsSwallowed:
    """`discover_wsd` 的 `except Exception` 兜底：失败 ⇒ 返回空列表，不抛。"""

    def test_search_services_exception_returns_empty_list(self, monkeypatch):
        """searchServices 抛异常 ⇒ 返回 []（**不是**把异常抛出去）。"""
        monkeypatch.setitem(
            sys.modules, "wsdiscovery",
            _fake_wsd_module(search_error=RuntimeError("WS-Discovery socket died")))

        result = wsd_engine.discover_wsd(timeout=0.1)

        assert isinstance(result, list), f"必须返回列表，实际 {type(result).__name__}"
        assert result == [], f"探测失败时应返回空列表，实际 {result!r}"

    def test_search_services_exception_still_stops_discovery(self, monkeypatch):
        """finally 里的 `wsd.stop()` 必须在失败路径上**也**执行（不泄漏套接字）。"""
        calls = []
        monkeypatch.setitem(
            sys.modules, "wsdiscovery",
            _fake_wsd_module(search_error=RuntimeError("boom"), calls=calls))

        wsd_engine.discover_wsd(timeout=0.1)

        assert calls == ["init", "start", "search:0.1", "stop"], (
            "调用序不符（失败路径也必须 stop）：%r" % (calls,))

    def test_start_exception_returns_empty_list(self, monkeypatch):
        """`wsd.start()` 抛异常 ⇒ 同样收敛成空列表。"""
        monkeypatch.setitem(
            sys.modules, "wsdiscovery",
            _fake_wsd_module(start_error=OSError("cannot bind multicast socket")))

        result = wsd_engine.discover_wsd(timeout=0.1)

        assert result == [], f"start 失败时应返回空列表，实际 {result!r}"

    def test_constructor_exception_returns_empty_list(self, monkeypatch):
        """`WSDiscovery()` 构造失败 ⇒ 同样收敛成空列表。"""
        monkeypatch.setitem(
            sys.modules, "wsdiscovery",
            _fake_wsd_module(init_error=RuntimeError("no such interface")))

        result = wsd_engine.discover_wsd(timeout=0.1)

        assert result == [], f"构造失败时应返回空列表，实际 {result!r}"

    def test_library_not_installed_returns_empty_list(self, monkeypatch):
        """WSDiscovery 库**没装**（ImportError）⇒ 空列表，不抛。"""
        # CPython 语义：sys.modules[name] = None ⇒ import 该模块抛 ImportError
        monkeypatch.setitem(sys.modules, "wsdiscovery", None)

        result = wsd_engine.discover_wsd(timeout=0.1)

        assert result == [], f"库缺失时应返回空列表，实际 {result!r}"

    def test_broken_service_is_skipped_and_others_survive(self, monkeypatch):
        """单条服务解析失败 ⇒ 跳过该条，其余服务**照常**产出（内层 except）。"""
        services = [
            _FakeService(xaddrs=["http://192.168.7.77:80/eSCL"],
                         display_name="坏服务", raise_on_xaddrs=True),
            _FakeService(xaddrs=["http://192.168.7.88:80/eSCL"],
                         display_name="好服务"),
        ]
        monkeypatch.setitem(sys.modules, "wsdiscovery",
                            _fake_wsd_module(services=services))
        monkeypatch.setattr(wsd_engine, "probe_escl",
                            lambda ip, **kw: "http://%s/eSCL" % ip)
        monkeypatch.setattr(wsd_engine, "fetch_capabilities", lambda s, **kw: s)

        result = wsd_engine.discover_wsd(timeout=0.1)

        assert [s.ip for s in result] == ["192.168.7.88"], (
            "坏服务应被跳过、好服务应保留，实际 %r" % ([s.ip for s in result],))


# ── 正向对照：证明 mock 真的生效（否则上面的"空列表"可能是假绿）──

class TestDiscoverWsdPositiveControl:
    """★ 非空转验证的前提：mock 生效时必须真的能产出扫描仪。"""

    def test_successful_discovery_returns_scanner(self, monkeypatch):
        services = [_FakeService(xaddrs=["http://192.168.7.77:80/eSCL"],
                                  display_name="HP LaserJet 收集")]
        monkeypatch.setitem(sys.modules, "wsdiscovery",
                            _fake_wsd_module(services=services))
        # 打桩网络层：绝不发真实 HTTP
        monkeypatch.setattr(wsd_engine, "probe_escl",
                            lambda ip, **kw: "http://%s/eSCL" % ip)
        monkeypatch.setattr(wsd_engine, "fetch_capabilities", lambda s, **kw: s)

        result = wsd_engine.discover_wsd(timeout=0.1)

        assert len(result) == 1, (
            "mock 生效时必须产出 1 台扫描仪（否则前面的'空列表'断言是假绿）：%r"
            % (result,))
        scanner = result[0]
        assert scanner.ip == "192.168.7.77"
        assert scanner.name == "HP LaserJet 收集"
        assert scanner.escl_url == "http://192.168.7.77/eSCL"
        # http:// 开头 ⇒ port 必须是 80（不是默认瞎填的 443）
        assert scanner.port == 80, "端口应由 escl_url 的协议推出，实际 %r" % scanner.port

    def test_timeout_is_passed_through_to_search_services(self, monkeypatch):
        """timeout 必须原样传给 searchServices（超时语义不能丢）。"""
        calls = []
        monkeypatch.setitem(sys.modules, "wsdiscovery",
                            _fake_wsd_module(calls=calls))

        wsd_engine.discover_wsd(timeout=2.5)

        assert "search:2.5" in calls, "timeout 未透传：%r" % (calls,)
