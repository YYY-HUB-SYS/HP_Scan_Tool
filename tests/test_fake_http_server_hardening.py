r"""
假 HTTP server 加固的回归护栏 —— HP Scan Tool v4.0

★ 背景（2026-10-21 实测根因，详见 `tests/fake_http_server.py` 顶部）
------------------------------------------------------------------
`tests/test_capabilities_known.py` 原先直接用裸
`http.server.ThreadingHTTPServer` 起假 eSCL 设备。在**全量上下文**里，
这个组合会**必现**地把整个 pytest 进程干掉（实测：6 文件子集 5/5 全崩，
15~18 秒一轮，无并发也能复现；栈逐字相同 ⇒ 客户端等 status line、
服务端 handler 卡在 sendall ⇒ 双向阻塞）。

根因链（实测坐实）：
  ① `HTTPServer.allow_reuse_address = 1` ⇒ 设 `SO_REUSEADDR`
  ② **Windows 上 `SO_REUSEADDR` 允许两个 socket 绑同一端口**（实测）
  ③ ⇒ 两个假 server 共享端口 ⇒ 连接被投递到其中某一个
  ④ ⇒ 客户端等 status line、另一个 handler 卡在 sendall ⇒ 双向阻塞
  ⑤ ⇒ socket 读超时（实测已装上）也不触发 ⇒ 进程 abort / 永久挂死

本文件把 ②③ 的**必要条件**钉成断言：端口必须**独占**。
"""

import ast
import os
import socket
import struct
import time

import pytest

import fake_http_server
from fake_http_server import (FakeHttpServer, SafeHandler, fake_escl_server,
                              fake_http_server as _fake_http_server_alias,
                              make_handler)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


# ── ① 端口独占（根因的直接护栏）──────────────────────────────────────


def test_server_class_disables_socket_reuse():
    """假 server **不得**开 SO_REUSEADDR / SO_REUSEPORT。

    Windows 上这两个选项都允许「两个 socket 绑同一端口」
    ⇒ 假 server 互相抢连接 ⇒ 客户端与 handler 双向阻塞 ⇒ 进程 abort。
    """
    srv_cls = fake_http_server._ExclusiveThreadingHTTPServer
    assert srv_cls.allow_reuse_address is False, "SO_REUSEADDR 会导致端口共享（根因）"
    assert srv_cls.allow_reuse_port is False, "SO_REUSEPORT 同样会导致端口共享"


def test_listening_socket_is_exclusive_in_practice():
    """实证：假 server 监听的端口**不能被第二个 socket 绑走**。

    这条比类属性断言更硬：它直接验证内核层面的独占语义。
    （对照：带 SO_REUSEADDR 的第二个 socket 能绑成功 ⇒ 会抢连接。）
    """
    handler = make_handler(b"<ok/>")
    srv = FakeHttpServer(handler).start()
    try:
        other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                other.bind(("127.0.0.1", srv.port))
                other.listen(1)
        finally:
            other.close()
    finally:
        srv.stop()


def test_reuseaddr_socket_could_steal_the_port():
    """对照实验：**两个都设** `SO_REUSEADDR` 的 socket **能**绑同一端口。

    ⇒ 这就是本轮崩溃的根因前提（Windows 特有）。
    而我们的假 server **不设** ⇒ 独占 ⇒ 即使对手设了 `SO_REUSEADDR`
    也绑不上（见上一条用例）。

    ★ 这条把「Windows 允许端口共享」这个前提本身钉进测试 ——
    如果哪天平台行为变了，这条会红，提示要重新评估加固策略。
    """
    first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    first.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    first.bind(("127.0.0.1", 0))
    first.listen(5)
    port = first.getsockname()[1]
    second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    second.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        second.bind(("127.0.0.1", port))    # ★ 不抛异常才是预期
        second.listen(5)
    finally:
        second.close()
        first.close()


def test_exclusive_socket_cannot_be_stolen_by_reuseaddr_peer():
    """我们的独占端口：即便对手设了 `SO_REUSEADDR` 也绑不上。

    ⇒ 独占是**双向**的（Windows 规则：只有双方都设 REUSEADDR 才共享）。
    """
    handler = make_handler(b"<ok/>")
    srv = FakeHttpServer(handler).start()
    try:
        other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            with pytest.raises(OSError):
                other.bind(("127.0.0.1", srv.port))
                other.listen(1)
        finally:
            other.close()
    finally:
        srv.stop()


# ── ② teardown 不得被卡死的 handler 线程拖住 ─────────────────────────


def test_server_close_does_not_join_handler_threads():
    """`block_on_close = False` ⇒ `server_close()` 不会 join handler 线程。

    否则一个卡在 sendall 里的 handler 会让整个 pytest 进程在 teardown 挂死。
    """
    srv_cls = fake_http_server._ExclusiveThreadingHTTPServer
    assert srv_cls.block_on_close is False
    assert srv_cls.daemon_threads is True, "daemon 线程不阻止解释器退出"


def test_stop_returns_promptly_even_with_a_pending_handler():
    """有连接挂着时 `stop()` 也能立刻返回（不 join 卡死线程）。"""
    handler = make_handler(b"<ok/>", delay=30.0)   # handler 会睡 30 秒
    srv = FakeHttpServer(handler).start()
    client = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
    try:
        client.sendall(b"GET /x HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(0.3)                              # 让 handler 进入 delay
        t0 = time.time()
        srv.stop()
        assert time.time() - t0 < 10, "stop() 不得等 handler 线程"
    finally:
        try:
            client.close()
        except OSError:
            pass


# ── ③ 客户端提前断开不得炸成未捕获线程异常 ───────────────────────────


def test_client_reset_does_not_raise_unhandled_thread_exception(recwarn):
    """客户端 RST 掉连接时，handler 线程**不得**抛未捕获异常。

    ★ 裸实现（`http.server.BaseHTTPRequestHandler`）在这里会抛
    `ConnectionResetError` ⇒ pytest 报 `PytestUnhandledThreadExceptionWarning`
    （实测全量跑里长期挂着这 1 条 warning）。
    """
    handler = make_handler(b"<payload/>", delay=1.5)
    srv = FakeHttpServer(handler).start()
    try:
        client = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        client.sendall(b"GET /x HTTP/1.1\r\nHost: x\r\n\r\n")
        # SO_LINGER(1,0) ⇒ close() 直接发 RST
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()
        time.sleep(3.0)          # 等 handler 睡完并尝试写
    finally:
        srv.stop()

    bad = [w for w in recwarn.list
           if "UnhandledThreadException" in type(w.message).__name__
           or "Unhandled thread exception" in str(w.message)]
    assert not bad, "handler 线程抛了未捕获异常: %s" % [str(w.message) for w in bad]


# ── ④ 服务端 I/O 超时：handler 不得永久阻塞 ───────────────────────────


def test_server_side_read_has_a_timeout(monkeypatch):
    """客户端连上不说话 ⇒ 服务端必须自己超时断开，而不是永久挂着。"""
    monkeypatch.setattr(fake_http_server, "SERVER_IO_TIMEOUT", 1.0)
    handler = make_handler(b"<ok/>")
    srv = FakeHttpServer(handler).start()
    client = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
    try:
        client.settimeout(15)
        t0 = time.time()
        data = client.recv(64)          # 服务端超时后应关闭连接 ⇒ 返回 b""
        dt = time.time() - t0
        assert data == b"", "服务端超时后应关闭连接"
        assert dt < 10, "服务端 I/O 超时未生效（等了 %.1fs）" % dt
    finally:
        try:
            client.close()
        except OSError:
            pass
        srv.stop()


# ── ⑤ 形态与静态守护 ─────────────────────────────────────────────────


def test_fake_escl_server_shape():
    """`fake_escl_server` 产出 `base + "/eSCL/"`。"""
    with fake_escl_server(b"<caps/>") as base:
        assert base.startswith("http://127.0.0.1:")
        assert base.endswith("/eSCL/")


def test_fake_http_server_base_url_shape():
    with _fake_http_server_alias(b"<ok/>") as base:
        assert base.startswith("http://127.0.0.1:")
        # 形如 http://127.0.0.1:54321 ⇒ 两个冒号（协议 + 端口）
        assert base.count(":") == 2


def test_safe_handler_sets_connection_close():
    """响应带 `Connection: close` ⇒ handler 不留悬挂线程与 TIME_WAIT 端口。"""
    assert SafeHandler.protocol_version == "HTTP/1.1"
    src = __import__("inspect").getsource(fake_http_server.SafeHandler.send_payload)
    assert "Connection" in src and "close" in src


def test_no_raw_threading_httpserver_in_tests():
    """静态守护：`tests/` 下**不许**再出现裸 `http.server.ThreadingHTTPServer`。

    ★ 那是本轮崩溃的原始写法。允许 `fake_http_server` 里那个
    `_ExclusiveThreadingHTTPServer`（名字不同，属性不同）。
    """
    offenders = []
    for name in sorted(os.listdir(TESTS_DIR)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(TESTS_DIR, name)
        with open(path, "r", encoding="utf-8") as fh:
            try:
                tree = ast.parse(fh.read(), filename=path)
            except SyntaxError as e:
                offenders.append("%s: 语法错误 %s" % (name, e))
                continue
        for node in ast.walk(tree):
            # ★ 只扫「实例化」（Call），不扫「继承」——
            # `fake_http_server.py` 里以它为基类做加固子类是允许的。
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "ThreadingHTTPServer"):
                offenders.append("%s:%d" % (name, node.lineno))
    assert not offenders, (
        "tests/ 下不得再直接用 http.server.ThreadingHTTPServer"
        "（Windows 上会端口共享 ⇒ 进程 abort/挂死）；改用 fake_http_server：%s"
        % offenders)
