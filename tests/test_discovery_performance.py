"""
发现性能契约测试 — HP Scan Tool v4.0

锁死 **2026-10-22 用户实测的「打印机加载延迟」**：
用户在新装的另一台电脑上打开软件，设备列表是空的，点「刷新」要等十几秒。

★ 查出来的真因（三条，缺一不可）：
  ① **WSD 发现串行探测**：内网 WSD 服务有 13~16 个（去重 12~14 个 IP），
     其中只有 4 台是扫描仪 ⇒ 旧实现对每个 IP **串行**做 `probe_escl`，
     而不可达 IP 会吃满超时（还含一次唤醒重试）⇒ 单 IP 最高 7.5 秒
     ⇒ 实测整轮 **44~51 秒**（timeout=4 实际 43.98s / timeout=8 实际 51.15s）。
  ② **`probe_escl` 的 timeout 不是总预算**：它对 2 个 base 各发一次请求、
     每次都吃满 timeout ⇒ 传 1.5 秒实际 3.02~5.66 秒。
  ③ **`discover_all_scanners` 的 join 下限写死 10 秒**：
     `t.join(max(timeout + 2, 10))` ⇒ 哪怕调用方只给 3 秒，也要等满 10 秒
     ⇒ 用户点「刷新」**每次至少 10 秒**。

★ 修完的实测数字（本机）：
     WSD 整轮   44~51 秒 → **5.1 秒**
     合并发现    18.01 秒 → **5.17 秒**（timeout=3，发现台数稳定 4 台）
"""

import ast
import io
import os
import threading
import time
from unittest import mock

import pytest

import wsd_engine


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeService:
    def __init__(self, ip):
        self._ip = ip

    def getXAddrs(self):
        return ["http://%s:80/eSCL/WSDScanner" % self._ip]


class _FakeWSDiscovery:
    """假 WSDiscovery：立刻返回一批服务。"""

    def __init__(self, ips):
        self._ips = ips
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def searchServices(self, timeout=None):
        return [_FakeService(ip) for ip in self._ips]

    def stop(self):
        self.stopped = True


class TestWsdProbesInParallel:
    """★ ① WSD 的候选 IP 探测必须**并行**（不能串行）"""

    def test_probes_run_concurrently(self, monkeypatch):
        """N 个候选 IP 的探测必须**同时**进行（峰值并发 > 1）"""
        import wsdiscovery

        ips = ["10.0.0.%d" % i for i in range(1, 9)]
        monkeypatch.setattr(wsdiscovery, "WSDiscovery",
                            lambda: _FakeWSDiscovery(ips))

        lock = threading.Lock()
        state = {"cur": 0, "peak": 0}

        def fake_build(ip, svc, **kw):
            with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            time.sleep(0.25)          # 假装在等网络
            with lock:
                state["cur"] -= 1
            return None

        monkeypatch.setattr(wsd_engine, "_build_scanner_from_wsd", fake_build)

        wsd_engine.discover_wsd(timeout=1.0)

        assert state["peak"] > 1, (
            "候选 IP 的探测是**串行**的（峰值并发=%d）⇒ 内网十几个 IP 时必然"
            "几十秒" % state["peak"])

    def test_probe_timeout_is_bounded(self):
        """单个候选 IP 的探测超时必须有界（<= 1.5 秒）"""
        assert wsd_engine._WSD_PROBE_TIMEOUT <= 1.5, (
            "单 IP 探测超时 %s 秒 ⇒ 每个不可达 IP 都要等这么久"
            % wsd_engine._WSD_PROBE_TIMEOUT)

    def test_wsd_probe_disables_wake_retry(self):
        """★ WSD 批量探测必须**关掉唤醒重试**（单 IP 最高 7.5 秒）"""
        import inspect
        src = inspect.getsource(wsd_engine.discover_wsd)
        assert "wake_retry=False" in src, (
            "WSD 探测没关唤醒重试 ⇒ 不可达 IP 每个都要多等 1.5s sleep + 再一轮")


class TestDiscoverAllScannersJoin:
    """★ ③ join 不许有 10 秒的硬编码下限"""

    def _code_only(self):
        """只取**代码行**（去掉注释）—— 注释里可以写"原来是 max(timeout+2,10)"。"""
        src = io.open(os.path.join(REPO_ROOT, "wsd_engine.py"),
                      encoding="utf-8").read()
        return "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))

    def test_no_hardcoded_ten_second_floor(self):
        code = self._code_only()
        assert "max(timeout + 2, 10)" not in code, (
            "join 的下限 10 秒是硬编码的 ⇒ timeout=3 也要等满 10 秒")
        assert "t.join(timeout + 4)" in code, "join 应当随 timeout 走"

    def test_join_is_proportional_to_timeout(self):
        """源码里不得出现与 timeout 无关的大常数 join"""
        code = self._code_only()
        for bad in ("join(10)", "join(15)", "join(20)"):
            assert bad not in code, "发现阶段出现固定长等待: %s" % bad


class TestGuiAutoDiscoveryOnFreshInstall:
    """★ 新装的机器（没有已保存设备）⇒ 启动时**自动**发现"""

    def _startup_ast(self):
        with io.open(os.path.join(REPO_ROOT, "hp_scan_gui_pyside.py"),
                     "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_do_startup_scan":
                return node
        raise AssertionError("找不到 _do_startup_scan()")

    def test_startup_calls_auto_discover(self):
        """启动流程里必须有 `_auto_discover` 的调用"""
        node = self._startup_ast()
        calls = [ast.unparse(n) for n in ast.walk(node)
                 if isinstance(n, ast.Call)]
        assert any("_auto_discover" in c for c in calls), (
            "启动流程没有触发自动发现 ⇒ 新机器打开就是空列表")

    def test_auto_discover_is_triggered_only_without_saved_devices(self):
        """★ 自动发现**只在「没有已保存设备」时**触发（已有设备的机器保持原行为）"""
        node = self._startup_ast()
        # 找 `if not saved:` 那个分支
        branch = None
        for n in ast.walk(node):
            if isinstance(n, ast.If):
                txt = ast.unparse(n.test)
                if "not saved" in txt:
                    branch = n
                    break
        assert branch is not None, "找不到 `if not saved:` 分支"
        calls = [ast.unparse(c) for c in ast.walk(branch)
                 if isinstance(c, ast.Call)]
        assert any("_auto_discover" in c for c in calls), (
            "没有已保存设备时必须自动发现")

    def test_auto_discover_is_gated_by_the_real_startup_flag(self):
        """★ 自动发现必须**受闸门控制**（只有 main() 打开的那一个）"""
        node = self._startup_ast()
        src = ast.unparse(node)
        assert "_allow_startup_autodiscover" in src, (
            "自动发现没有闸门 ⇒ 单元测试直接构造 ScanApp 时会跑**真实 mDNS/WSD 发现**"
            "（既违反「测试不发真实网络」，又把台账 §12 的 pytest abort 触发出来）")


class TestNoRealDiscoveryInTestProcess:
    """★ 单元测试进程里**绝不能**跑起真实发现"""

    def test_constructing_scanapp_does_not_start_discovery(self, qt_app, monkeypatch):
        import time

        import hp_scan_gui_pyside as gui

        calls = []
        monkeypatch.setattr(gui.ScanApp, "_auto_discover",
                            lambda self: calls.append(1))
        monkeypatch.setattr(gui.ScanApp, "_finish_loading", lambda self: None)
        win = gui.ScanApp()
        try:
            t0 = time.time()
            while time.time() - t0 < 0.4:      # 让 100ms 的 singleShot 触发
                qt_app.processEvents()
                time.sleep(0.02)
            assert calls == [], (
                "测试进程里触发了自动发现 ⇒ 会跑真实 mDNS/WSD 发现")
        finally:
            win.deleteLater()
