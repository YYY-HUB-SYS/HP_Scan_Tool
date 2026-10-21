r"""
测试用本地假 HTTP 服务（假 eSCL 设备替身）—— **抗挂死加固版**

★ 为什么需要这个模块（2026-10-21 实测）
------------------------------------------------------------------
`tests/test_capabilities_known.py` 原先直接用
`http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)` 起假设备。
在**全量上下文**里，这个组合会**低频但反复**地把整个 pytest 进程干掉
（实测：6 文件子集 5/5 全崩，15~18 秒一轮；★ 订正：用 worktree 把同一份旧代码
搬到安静机器上重测是 **6/6 不崩** ⇒ 这是**概率事件（约 5%~25%）**，
不是代码的稳定属性）。

崩溃栈每一轮逐字相同：

    主线程    socket.readinto → http/client._read_status → requests.get
              → escl_engine.fetch_capabilities → test_successful_query_marks_known
              （客户端在等 status line）
    服务端线程 socketserver.write(sendall) → http/server.flush_headers
              → end_headers → do_GET
              （服务端正要把 status line 发出去）

★ **机制尚未定位**（独立 verifier 复核后订正了此前的说法，勿照抄旧版）：

- ⚠️ **此前这里写「根因 = 同端口双监听 ⇒ 连接被投递到另一个 listener ⇒
  两端错配」—— 这一条被实测否掉了**：Windows 上双 bind 确实成功，
  但连接**只会投递给先绑的那个 listener** ⇒ 非胜出 listener 根本没有连接
  ⇒ **双监听本身不足以产生互锁**。
- ⇒ 下面是**已知事实**与**强线索**，不是已证根因：
  1. `http.server.HTTPServer.allow_reuse_address = 1` ⇒ 监听 socket 设了
     `SO_REUSEADDR`。**Windows 上它允许两个 socket 绑同一个端口**
     （实测：第二个 bind 成功；不设则 10048 独占失败）⇒ 必须消除这个危险面。
  2. 旧版 handler 用 `protocol_version = "HTTP/1.1"`（**keep-alive**）且响应
     **不带 `Connection: close`** ⇒ 服务端响应后**不关连接**，handler 线程回到
     `handle_one_request()` 阻塞在 `self.rfile.readline()` **等下一个请求**；
     而 `self.rfile` 是带**预读**的 BufferedReader（8192 字节）。
  3. `socketserver.ThreadingMixIn.server_close()` 会 `self._threads.join()`
     ⇒ 卡在 `readline()` 的 handler 会让 teardown **永久阻塞**。
  4. 客户端侧：urllib3 确实把 3 秒读超时装上了
     （`urllib3/connection.py:627` `sock.settimeout(self.timeout)`、
     `connectionpool.py:536` `conn.timeout = read_timeout`），但实测**卡了 9 分钟
     都没触发** ⇒ 读超时在此状态下不可靠。
  5. ⇒ 现象：客户端等 status line、服务端卡在 sendall ⇒ 双向阻塞 ⇒ 进程最终
     `Fatal Python error: Aborted` 或永久挂死（实测挂满 600 秒）。

**本模块的五道加固**（消除可触达条件；★ **不是「已根治」**）：

| 加固 | 消除的条件 |
|------|-----------|
| `allow_reuse_address = False` + `allow_reuse_port = False` | 端口**独占** ⇒ 同一端口上不会出现两个监听 socket（消除 §1 的危险面） |
| bind 撞端口（10048）时**换端口重试** | 独占后 bind 会**明确报错** ⇒ 必须能换端口而不是崩掉 |
| 服务端 socket `settimeout` | handler 线程不会永久阻塞在 send/recv |
| 响应带 `Connection: close` | 响应后即关连接 ⇒ handler 不再回到 `readline()` 等下一个请求（直接针对 §2、§3） |
| `block_on_close = False` + `daemon_threads = True` | teardown 不会 join 卡死的 handler 线程 ⇒ 不会把 pytest 挂住（直接针对 §3） |
| 吞掉「客户端已断开」类异常 | 消除实测出现过的 `PytestUnhandledThreadExceptionWarning` |

★ 用法（两个调用方统一走这里）：
    with fake_escl_server(CAPS_XML) as base:
        ...  # base 形如 http://127.0.0.1:54321
"""

import contextlib
import http.server
import socket
import threading
import time

#: 服务端单次 I/O 的上限（秒）—— handler 线程绝不允许无限期阻塞在 send/recv 上
SERVER_IO_TIMEOUT = 10.0

#: bind 撞端口时的重试次数与退避间隔（独占端口后端口冲突概率上升）
BIND_RETRIES = 6
BIND_RETRY_DELAY = 0.05

#: 客户端断开（读完了就走）是**正常现象**，不该炸成未捕获线程异常
_CLIENT_GONE = (
    BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
    socket.timeout, OSError,
)


class _ExclusiveThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """
    ★ 端口**独占**的 ThreadingHTTPServer。

    `allow_reuse_address` / `allow_reuse_port` 必须在**类属性**上关掉：
    `socketserver.TCPServer.server_bind()` 会在 `__init__` 里据此决定是否
    设置 `SO_REUSEADDR` / `SO_REUSEPORT`。

    Windows 上 `SO_REUSEADDR`（以及 `SO_REUSEPORT`）允许**两个 socket 绑同一端口**
    ⇒ 假 server 会互相「抢」连接 ⇒ 客户端与 handler 各等一头 ⇒ 双向阻塞。
    关掉之后端口是独占的，端口冲突会**明确报 10048**，由 `_bind_with_retry` 处理。
    """

    daemon_threads = True
    block_on_close = False        # ★ 不 join handler 线程（卡死的 handler 不能拖住 teardown）
    allow_reuse_address = False   # ★ 端口独占（消除 SO_REUSEADDR 共享危险面）
    allow_reuse_port = False      # ★ 同上（SO_REUSEPORT 同样允许共享）
    request_queue_size = 32


class SafeHandler(http.server.BaseHTTPRequestHandler):
    """
    抗挂死的 handler 基类。

    ★ 三点改动（相对 `http.server.BaseHTTPRequestHandler`）：
      1. `setup()` 里给服务端 socket 设 I/O 超时 ⇒ handler 线程不会永久阻塞；
      2. `send_payload()` 吞掉「客户端已断开」类异常 ⇒ 不产生未捕获线程异常
         （实测原实现会在全量跑里产出 `PytestUnhandledThreadExceptionWarning`）；
      3. 响应带 `Connection: close` ⇒ 每次请求一条连接 ⇒ handler 及时退出，
         不留悬挂线程与 TIME_WAIT 端口。
    """

    protocol_version = "HTTP/1.1"
    server_version = "FakeDevice/1.0"
    sys_version = ""

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(SERVER_IO_TIMEOUT)
        except OSError:
            pass

    def send_payload(self, body, status=200, content_type="application/xml",
                     delay=0.0):
        """把 `body` 发出去；客户端提前断开时静默收场（不算失败）。"""
        if delay:
            time.sleep(delay)
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            self.close_connection = True

    def log_message(self, *args):   # 静音访问日志
        pass


def make_handler(body=b"", status=200, content_type="application/xml", delay=0.0):
    """生成一个「回固定内容」的 handler 类。"""
    payload = body.encode("utf-8") if isinstance(body, str) else body

    class _Handler(SafeHandler):
        def do_GET(self):
            self.send_payload(payload, status=status,
                              content_type=content_type, delay=delay)

    return _Handler


class FakeHttpServer:
    """
    本地假 HTTP 服务。

    ★ 端口**独占**；bind 撞端口时换端口重试（而不是共享端口）。
    """

    def __init__(self, handler_cls, host="127.0.0.1", port=0):
        self.host = host
        self._srv = None
        last_err = None
        for _ in range(BIND_RETRIES):
            try:
                self._srv = _ExclusiveThreadingHTTPServer((host, port), handler_cls)
                break
            except OSError as e:
                # 10048 = 地址已在使用（独占后端口被占）⇒ 换端口重试
                last_err = e
                time.sleep(BIND_RETRY_DELAY)
        if self._srv is None:
            raise RuntimeError(
                "无法绑定本地假 HTTP 服务端口（重试 %d 次）：%r" % (BIND_RETRIES, last_err))
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(
            target=self._srv.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True)

    @property
    def base_url(self):
        return "http://%s:%d" % (self.host, self.port)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        try:
            self._srv.shutdown()
        finally:
            self._srv.server_close()
            self._thread.join(timeout=5)


@contextlib.contextmanager
def fake_http_server(body=b"", status=200, content_type="application/xml",
                     delay=0.0, host="127.0.0.1"):
    """起一个本地假 HTTP 服务，产出 base url；退出时**保证**收干净。"""
    handler = make_handler(body, status=status, content_type=content_type,
                           delay=delay)
    srv = FakeHttpServer(handler, host=host).start()
    try:
        yield srv.base_url
    finally:
        srv.stop()


@contextlib.contextmanager
def fake_escl_server(body=b"", status=200, delay=0.0, host="127.0.0.1"):
    """假 eSCL 设备（`/eSCL/...` 任意路径都回同一份能力文档）。"""
    with fake_http_server(body, status=status, delay=delay, host=host) as base:
        yield base + "/eSCL/"
