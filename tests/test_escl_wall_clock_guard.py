r"""
eSCL HTTP **墙钟看门狗**的回归护栏 —— HP Scan Tool v4.0

★ 为什么需要这条护栏（实测，2026-10-21）
  `requests` 的 read timeout 是 **socket 层**的。实测在「对端接受 TCP 连接但
  迟迟不回数据」的状态下，这个超时**可能长时间不触发**（本轮排查抓到过
  一个真实样本：socket 读超时 3 秒，**9 分钟都没有报错**，调用方与对端
  互相阻塞）。

  ⇒ 只靠 socket 超时不够：一台「半死不活」的扫描仪（503 pending job、
  打印机休眠、Busy）就能把 **GUI 的扫描线程永久卡死**，用户只能杀进程。

  ⇒ 因此 `escl_engine` 在 socket 超时之外**再加一道墙钟**
  （`_http_call_bounded`）：墙钟到期按 `requests.exceptions.ReadTimeout`
  抛出 ⇒ 上层 `_transport_error_to_device_error` 把它映射成中文可执行提示。

本文件钉住两件事：
  ① 墙钟机制**本身**有效（给一个永不返回的调用，墙钟必须到点抛错）；
  ② 真实 socket 场景下（「对端接受连接但不响应」）`fetch_capabilities`
     **不得无限期挂住**，且能力必须保持「未知」而不是被误判为已知。
"""

import socket
import threading
import time

import pytest
import requests

import escl_engine
from escl_engine import ScannerInfo


# ── ① 墙钟机制本身 ────────────────────────────────────────────────────


def test_wall_clock_fires_on_a_call_that_never_returns():
    """永不返回的调用 ⇒ 墙钟到点必须抛 ReadTimeout（不能一起挂死）。"""
    started = time.time()

    def _never_returns():
        time.sleep(60)
        return "unreachable"

    with pytest.raises(requests.exceptions.ReadTimeout):
        escl_engine._http_call_bounded(_never_returns, 1.0, "测试用永不返回的调用")

    elapsed = time.time() - started
    assert elapsed < 10, "墙钟没有及时触发（等了 %.1fs）" % elapsed


def test_wall_clock_passes_through_normal_result_and_exception():
    """正常返回值 / 正常异常都要**原样透传**（墙钟只是兜底，不能改变正常语义）。"""
    assert escl_engine._http_call_bounded(lambda: 42, 5.0, "正常返回") == 42

    def _boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        escl_engine._http_call_bounded(_boom, 5.0, "正常抛错")


def test_wall_clock_derived_from_timeout_tuple():
    """`timeout=(连接, 读)` ⇒ 墙钟要覆盖两者之和再加余量。"""
    assert escl_engine._wall_clock_for((2.0, 3.0)) >= 5.0
    assert escl_engine._wall_clock_for(3.0) >= 3.0
    assert escl_engine._wall_clock_for(None) > 0


# ── ② 真实 socket 场景：对端不响应 ────────────────────────────────────


class _SilentServer:
    """接受连接但**永不响应**的假设备（模拟半死扫描仪）。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._conns = []

    def start(self):
        def _loop():
            while not self._stop.is_set():
                try:
                    self.sock.settimeout(0.5)
                    conn, _addr = self.sock.accept()
                except (socket.timeout, OSError):
                    continue
                # 收了连接就**什么都不做** ⇒ 客户端读超时/墙钟到期
                self._conns.append(conn)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass


def test_fetch_capabilities_does_not_hang_when_peer_never_responds():
    """对端接受连接但不响应 ⇒ `fetch_capabilities` 必须**有界返回**。

    ★ 不管最终是 socket 读超时先触发、还是墙钟兜底触发，
      这里只钉住**结论**：调用方不会被永久挂住，且能力保持「未知」。
    """
    server = _SilentServer().start()
    try:
        scanner = ScannerInfo(name="silent", ip="127.0.0.1")
        scanner.escl_url = "http://127.0.0.1:%d/eSCL/" % server.port

        t0 = time.time()
        escl_engine.fetch_capabilities(scanner, timeout=1.0)
        elapsed = time.time() - t0

        assert elapsed < 25, "能力查询挂住了 %.1fs（未返回）" % elapsed
        assert scanner.capabilities_known is False, "没拿到能力文档却被判为已知"
    finally:
        server.stop()
