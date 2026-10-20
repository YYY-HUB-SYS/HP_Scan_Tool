"""
CLI 错误呈现 + 探测去重回归测试 — HP Scan Tool v4.0

锁死 F-1（Important）：`execute_scan` 抛出的异常，CLI 不得退化成 Python traceback
------------------------------------------------------------------
协议层（escl_engine）已经把"设备/协议类失败"的可执行指引写好了：
  - HTTP 409 →「打印机上残留了一个未完成的扫描任务……请在打印机面板上取消该任务，
    设备回收需要 15-20 分钟」
  - HTTP 503 →「打印机当前不可用……请确认输稿器里已放入纸张后重试」
  - 超时     →「扫描超时……请确认打印机已开机并完成初始化」
改造前 CLI 直接让异常逃逸 ⇒ 用户拿到的是 14 行 Python traceback（含本地绝对路径），
既看不懂也不知道下一步该干什么。⇒ CLI 必须把消息**原样**呈现给用户（stderr）。

同时锁死 F-2（Important）：CLI 的 `port=443` 第二轮探测与第一轮**整块重叠**
  ⇒ 同一个 URL 被重复请求。改造后 CLI 只探**一轮**（候选并入同一张表）。

两条纪律：
  - 错误信息只走 stderr，**绝不**进 stdout（stdout 要能被下游程序消费）；
  - 失败退出码必须非 0。
"""

import json
import os
import sys
from unittest import mock

import pytest
import requests

import cli
import escl_engine

# ★ 子进程级探针（复审 N-1）。
#   「stderr 里没有 traceback」这条判据**只能在子进程里**验：
#   pytest 给 root logger 挂了 LogCaptureHandler ⇒ Python 的兜底 handler
#   `logging.lastResort` 永不启用 ⇒ 进程内 capsys 看不到真实 CLI 进程的形态。
#   详见同目录 cli_subprocess_probe.py。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cli_subprocess_probe  # noqa: E402


# ── 测试替身 ────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _args(*argv):
    return cli._build_parser().parse_args(list(argv))


def _stub_probe(monkeypatch, url="http://192.168.1.2/eSCL/"):
    monkeypatch.setattr(cli, "probe_escl", lambda ip, **kw: url)
    monkeypatch.setattr(cli, "fetch_capabilities", lambda s, **kw: s)


def _real_409_error():
    """用**协议层真实路径**造出 409 错误对象（不是手写字符串）。"""
    scanner = escl_engine.ScannerInfo(name="t", ip="1.2.3.4")
    scanner.escl_url = "http://1.2.3.4:80/eSCL/"
    session = mock.MagicMock()
    session.post.return_value = _Resp(409, "busy")
    with mock.patch("escl_engine.time.sleep"):
        with pytest.raises(escl_engine.ScanDeviceError) as exc:
            escl_engine._create_scan_job(
                session, scanner, 300, "RGB24", "jpg", "Platen", False)
    return exc.value


def _real_503_error():
    """用协议层真实路径造出 503 错误对象。"""
    scanner = escl_engine.ScannerInfo(name="t", ip="1.2.3.4")
    scanner.escl_url = "http://1.2.3.4:80/eSCL/"
    session = mock.MagicMock()
    session.post.return_value = _Resp(503, "")
    with pytest.raises(escl_engine.ScanDeviceError) as exc:
        escl_engine._create_scan_job(
            session, scanner, 300, "RGB24", "jpg", "Feeder", False)
    return exc.value


def _raising_scan(monkeypatch, exc):
    def _boom(**kwargs):
        raise exc
    monkeypatch.setattr(cli, "execute_scan", _boom)


# ── F-1：可执行错误指引必须原样呈现 ──────────────────────


class TestActionableGuidanceSurfaced:
    def test_409_guidance_reaches_stderr(self, monkeypatch, capsys):
        """409（打印机上残留未完成任务）：stderr 必须出现可执行指引。"""
        _stub_probe(monkeypatch)
        _raising_scan(monkeypatch, _real_409_error())

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        assert rc != 0, "失败退出码必须非 0"
        err = capsys.readouterr().err
        assert "打印机面板" in err, "必须告诉用户去打印机面板取消任务"
        assert "15-20 分钟" in err, "必须告诉用户设备回收要多久"

    def test_409_guidance_has_no_python_traceback_in_real_process(self, tmp_path):
        """★ 子进程级判据（复审 N-1）：**真实 CLI 进程**的 stderr 不得带 traceback。

        原来的进程内断言是**假绿**：pytest 给 root logger 挂了
        `LogCaptureHandler` ⇒ `Logger.callHandlers` 一定找到 handler ⇒
        Python 的兜底 handler `logging.lastResort` **永不启用**
        ⇒ 进程内 capsys 看不到真实 CLI 进程会看到的那份 traceback。
        ⇒ 这条判据必须起**子进程**（`python -m cli ...`）验。
        """
        rc, out, err = cli_subprocess_probe.run_cli(
            ["scan", "--ip", "127.0.0.1"], mode="conflict",
            appdata=tmp_path / "appdata")

        assert rc != 0, "失败退出码必须非 0"
        assert out == "", f"失败时 stdout 必须为空，实际 {out!r}"
        assert "打印机面板" in err, "必须告诉用户去打印机面板取消任务"
        assert "15-20 分钟" in err, "必须告诉用户设备回收要多久"
        assert "Traceback" not in err, (
            "真实 CLI 进程的 stderr 不得带 traceback；实际 %r" % err)
        assert "  File " not in err, (
            "不该把源码路径甩给用户；实际 %r" % err)

    def test_503_adf_paper_guidance_reaches_stderr(self, monkeypatch, capsys):
        """503（输稿器空载）：stderr 必须说清"放纸"这个可执行动作。"""
        _stub_probe(monkeypatch)
        _raising_scan(monkeypatch, _real_503_error())

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        assert rc != 0
        err = capsys.readouterr().err
        assert "输稿器" in err
        assert "纸张" in err

    def test_timeout_guidance_reaches_stderr(self, monkeypatch, capsys):
        """超时（设备没响应）：stderr 必须有下一步，不只是"超时"两个字。"""
        _stub_probe(monkeypatch)
        _raising_scan(monkeypatch,
                      escl_engine.ScanDeviceError(escl_engine.SCAN_TIMEOUT_MESSAGE))

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        assert rc != 0
        err = capsys.readouterr().err
        assert "扫描超时" in err
        assert "开机" in err, "必须给出设备没响应之外的下一步"

    def test_stdout_stays_empty_on_failure(self, monkeypatch, capsys):
        """失败时 stdout 必须为空：进度提示已改走 stderr。"""
        _stub_probe(monkeypatch)
        _raising_scan(monkeypatch, _real_409_error())

        cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        cap = capsys.readouterr()
        assert cap.out == "", f"失败时 stdout 必须为空，实际 {cap.out!r}"
        assert "正在探测" not in cap.out
        assert "正在扫描" not in cap.out

    def test_plain_runtime_error_with_device_markers_is_actionable(
            self, monkeypatch, capsys):
        """协议层仍是普通 RuntimeError 的老路径：靠消息特征词判定，不甩 traceback。"""
        _stub_probe(monkeypatch)
        _raising_scan(monkeypatch, RuntimeError(
            "打印机上残留了一个未完成的扫描任务（HTTP 409），请稍后重试"))

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        assert rc != 0
        err = capsys.readouterr().err
        assert "HTTP 409" in err
        assert "Traceback" not in err

    def test_unexpected_error_keeps_traceback(self, monkeypatch, capsys):
        """意外错误（代码 bug 等）：保留 traceback 便于排障。"""
        _stub_probe(monkeypatch)
        _raising_scan(monkeypatch, ValueError("某个内部变量是 None"))

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        assert rc != 0
        err = capsys.readouterr().err
        assert "Traceback" in err, "意外错误必须保留 traceback"
        assert err.count("Traceback") == 1, (
            "意外错误的 traceback 只能打一次（复审 N-2），实际 %d 次" % err.count("Traceback"))
        assert "某个内部变量是 None" in err
        assert capsys.readouterr().out == ""


class TestJsonOutputStaysPure:
    def test_discover_json_stdout_is_pure_json(self, monkeypatch, capsys):
        """`--format json` 时 stdout 必须是纯 JSON（错误呈现改动不得污染它）。"""
        monkeypatch.setattr(cli, "discover_all_scanners", lambda timeout=4.0: [])
        assert cli.cmd_discover(_args("discover", "--format", "json")) == 0
        out = capsys.readouterr().out
        assert out.startswith("[")
        assert json.loads(out) == []

    def test_status_json_stdout_is_pure_json(self, monkeypatch, capsys):
        """status 的 json 分支：错误信息也必须包在 JSON 里（stdout 仍是纯 JSON）。"""
        monkeypatch.setattr(cli, "probe_escl",
                            lambda ip, **kw: "http://192.168.1.2/eSCL/")
        monkeypatch.setattr(cli, "get_scanner_status",
                            lambda url, **kw: (_ for _ in ()).throw(
                                RuntimeError("设备没响应")))
        rc = cli.cmd_status(_args("status", "--ip", "192.168.1.2", "--format", "json"))
        assert rc != 0
        out = capsys.readouterr().out
        assert json.loads(out)["error"] == "设备没响应"


# ── F-2：CLI 探测不得重复请求同一个 base ─────────────────


class TestProbeNoDuplicateRequests:
    @staticmethod
    def _record_urls(monkeypatch, responder):
        seen = []

        def _fake_get(url, **kw):
            seen.append(url)
            return responder(url)

        monkeypatch.setattr("escl_engine.requests.get", _fake_get)
        return seen

    def test_probe_is_called_only_once(self, monkeypatch, capsys):
        """cmd_scan 只探**一轮**（原来分两轮，第二轮与第一轮整块重叠）。"""
        calls = []

        def _fake_probe(ip, **kw):
            calls.append((ip, kw))
            return None

        monkeypatch.setattr(cli, "probe_escl", _fake_probe)
        monkeypatch.setattr(cli, "fetch_capabilities", lambda s, **kw: s)

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        assert rc != 0
        assert len(calls) == 1, f"只应探测一次，实际 {len(calls)} 次"

    def test_no_url_is_requested_twice_when_unreachable(self, monkeypatch, capsys):
        """设备不可达：每个 URL 只被请求一次（连接被拒形态）。"""
        # 注意：这里**不能**打桩 cli.probe_escl —— 要让真实的探测跑起来，
        # 才能数到真实的请求 URL。
        seen = self._record_urls(
            monkeypatch, lambda url: (_ for _ in ()).throw(
                requests.ConnectionError("unreachable")))

        cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        assert seen, "应当发起过探测请求"
        assert len(seen) == len(set(seen)), f"同一 URL 被重复请求：{seen}"

    def test_no_url_is_requested_twice_when_404(self, monkeypatch, capsys):
        """设备端口通但不是 eSCL（404 形态）：每个 URL 只被请求一次。"""
        seen = self._record_urls(monkeypatch, lambda url: _Resp(404, "nope"))

        cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        assert seen, "应当发起过探测请求"
        assert len(seen) == len(set(seen)), f"同一 URL 被重复请求：{seen}"

    def test_device_on_http_443_is_still_discovered(self, monkeypatch, capsys):
        """覆盖面不缩水：只在 http:443 上提供 eSCL 的设备仍能被探到。"""
        used = {}

        def _fake_get(url, **kw):
            if url == "http://192.168.1.2:443/eSCL/ScannerStatus":
                return _Resp(200, "<xml/>")
            return _Resp(404, "nope")

        monkeypatch.setattr("escl_engine.requests.get", _fake_get)

        def _fake_scan(**kwargs):
            used["url"] = kwargs["scanner"].escl_url
            raise RuntimeError("stop-here")

        monkeypatch.setattr(cli, "execute_scan", _fake_scan)
        monkeypatch.setattr(cli, "fetch_capabilities", lambda s, **kw: s)

        cli.cmd_scan(_args("scan", "--ip", "192.168.1.2"))

        assert used.get("url") == "http://192.168.1.2:443/eSCL/", \
            f"http:443 的设备必须仍能被发现，实际 {used.get('url')}"

    def test_status_also_probes_once(self, monkeypatch, capsys):
        """cmd_status 同样只探一轮。"""
        calls = []

        def _fake_probe(ip, **kw):
            calls.append((ip, kw))
            return None

        monkeypatch.setattr(cli, "probe_escl", _fake_probe)

        assert cli.cmd_status(_args("status", "--ip", "192.168.1.2")) != 0
        assert len(calls) == 1, f"只应探测一次，实际 {len(calls)} 次"
