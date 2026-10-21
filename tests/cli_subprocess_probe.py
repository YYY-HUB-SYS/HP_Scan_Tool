"""
CLI 子进程探针 —— 用**真实 CLI 进程**复现 stderr 形态。

为什么必须起子进程（而不是在 pytest 里 capsys 断言）
------------------------------------------------------
`logging.lastResort` 是 Python 的兜底 handler：**只有当一条日志记录在整条
logger 链上找不到任何 handler 时**才会启用，把记录（连同 `exc_info` 里的
traceback）直接写进 stderr。

pytest 在每个测试期给 root logger 挂 `LogCaptureHandler`
⇒ `Logger.callHandlers` 一定 `found >= 1`
⇒ `lastResort` **永不启用**
⇒ 进程内 capsys 看不到真实 CLI 进程会看到的形态
⇒ 「stderr 里没有 traceback」这条判据在进程内断言 = **假绿**（复审 N-1）。

所以本模块提供的 `run_cli()` 走的是**真实入口**：`sys.executable -m cli ...`
⇒ 独立的解释器 ⇒ 干净的 logging 状态 ⇒ 能真正复现 `logging.lastResort`。

两件东西：
  - `fake_escl_server()`：测试进程内的**本地假 eSCL 设备**（真实 HTTP，
    POST /eSCL/ScanJobs 真的回 409 ⇒ 协议层抛的是**真实**的 ScanDeviceError）；
  - `run_cli()`：拉起真实 CLI 子进程，返回解码后的 (returncode, stdout, stderr)。

用户数据隔离：子进程通过 `APPDATA` 环境变量指向调用方给的沙箱目录，
绝不碰真实的 `%APPDATA%\\HP_Scan_Tool`。
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from fake_http_server import FakeHttpServer, SafeHandler

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 假设备的 ScannerStatus 响应（够 fetch_capabilities 回退路径解析即可）
_STATUS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03">'
    '<scan:ScannerState>Processing</scan:ScannerState>'
    '</scan:ScannerStatus>'
).encode("utf-8")

# 注入到子进程的 sitecustomize：只做两件事
#   ① 把 `probe_escl` 指向本地假设备（否则 CLI 只会去探 80/443 端口）；
#   ② mode=bug 时让 `execute_scan` 抛一个**意外错误**。
# 另外把 409 的退避秒数清零（2/4/8 共 14 秒）—— 只为让用例秒级返回，
# **报错文案与触发路径都是协议层的真实代码**（escl_engine._create_scan_job）。
_SITECUSTOMIZE = '''# -*- coding: utf-8 -*-
"""子进程侧注入：把 CLI 的 eSCL 探测指向测试进程里的本地假设备。

本文件在 `import cli` **之前**执行 ⇒ cli.py 里
`from escl_engine import probe_escl, execute_scan` 绑定到的就是打过补丁的版本。
"""
import os

import escl_engine

_PORT = os.environ.get("HP_SCAN_FAKE_ESCL_PORT", "")
_MODE = os.environ.get("HP_SCAN_FAKE_MODE", "conflict")
_MARKER = os.environ.get("HP_SCAN_FAKE_MARKER", "")


def _fake_probe_escl(ip, port=None, timeout=2.0, extra_ports=()):
    return "http://127.0.0.1:%s/eSCL/" % _PORT


escl_engine.probe_escl = _fake_probe_escl
# 409 退避（2/4/8 秒共 14 秒）清零：只为让用例秒级返回，报错文案不变
escl_engine._CONFLICT_BACKOFF_SECONDS = ()

if _MODE == "bug":
    def _fake_execute_scan(*args, **kwargs):
        raise ValueError("某个内部变量是 None")

    escl_engine.execute_scan = _fake_execute_scan

if _MARKER:
    with open(_MARKER, "w", encoding="utf-8") as _fh:
        _fh.write("ok")
'''


class _FakeEsclHandler(SafeHandler):
    """假 eSCL 设备：ScannerStatus 回 200，ScanJobs 回 409。

    ★ 继承 `SafeHandler`（而不是裸 `http.server.BaseHTTPRequestHandler`）：
    拿到「服务端 I/O 超时 + 客户端断开不炸线程 + Connection: close」三项加固。
    裸实现曾让整轮 pytest 因「假 server 抢端口 ⇒ 客户端与 handler 双向阻塞」
    而 abort（实测 5/5 必现），根因见 `tests/fake_http_server.py` 顶部。
    """

    def _reply(self, code, body=b""):
        self.send_payload(body, status=code, content_type="text/xml")

    def do_GET(self):
        if self.path.endswith("/ScannerStatus"):
            self._reply(200, _STATUS_XML)
        else:
            self._reply(404, b"not found")

    def do_POST(self):
        # 打印机上残留未完成任务 → 协议层据此给出 409 + 可执行指引
        self._reply(409, b"busy")


class FakeEsclServer:
    """本地假 eSCL 设备（监听 127.0.0.1 的随机端口，**端口独占**）。"""

    def __init__(self):
        self._srv = FakeHttpServer(_FakeEsclHandler)
        self.port = self._srv.port

    def start(self):
        self._srv.start()
        return self

    def stop(self):
        self._srv.stop()


def run_cli(argv, mode="conflict", appdata=None, timeout=90):
    """
    拉起真实 CLI 子进程（`sys.executable -m cli ...`），返回
    (returncode, stdout, stderr)。

    - `mode="conflict"`：假设备对 POST /eSCL/ScanJobs 回 409
      ⇒ 协议层抛出**带可执行指引**的 ScanDeviceError（可操作错误）；
    - `mode="bug"`：execute_scan 直接抛 ValueError（意外错误）。

    stdout/stderr 一律按 **UTF-8** 解码（`errors="replace"`）：
    子进程侧用 `PYTHONIOENCODING=utf-8` 保证输出的是 UTF-8 字节，
    避免 Windows 控制台默认 GBK 把中文变成乱码导致断言失真。
    """
    project_root = str(PROJECT_ROOT)
    fake_dir = tempfile.mkdtemp(prefix="hp_scan_fake_cli_")   # %TEMP%
    marker = os.path.join(fake_dir, "injected.marker")
    appdata_dir = pathlib.Path(appdata) if appdata else pathlib.Path(fake_dir) / "appdata"
    appdata_dir.mkdir(parents=True, exist_ok=True)

    try:
        (pathlib.Path(fake_dir) / "sitecustomize.py").write_text(
            _SITECUSTOMIZE, encoding="utf-8")

        server = FakeEsclServer().start()
        try:
            env = os.environ.copy()
            env["APPDATA"] = str(appdata_dir)              # 用户数据隔离
            env["PYTHONPATH"] = os.pathsep.join([fake_dir, project_root])
            env["PYTHONIOENCODING"] = "utf-8"              # 输出按 UTF-8
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["HP_SCAN_FAKE_ESCL_PORT"] = str(server.port)
            env["HP_SCAN_FAKE_MODE"] = mode
            env["HP_SCAN_FAKE_MARKER"] = marker

            proc = subprocess.run(
                [sys.executable, "-m", "cli"] + list(argv),
                cwd=project_root, env=env,
                capture_output=True, timeout=timeout,
            )
        finally:
            server.stop()

        # 自检：sitecustomize 真的被加载了（否则子进程会去打真实网络，
        # 失败形态完全不同 ⇒ 断言会失真）。必须在清理临时目录**之前**查。
        injected = os.path.exists(marker)
    finally:
        shutil.rmtree(fake_dir, ignore_errors=True)

    if not injected:
        raise AssertionError(
            "sitecustomize 未被加载（marker 缺失）—— 子进程环境不对，"
            "本次断言不可信。stderr=%r" % proc.stderr[:2000])

    return (proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))
