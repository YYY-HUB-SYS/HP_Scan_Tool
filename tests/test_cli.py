"""
cli.py 单元测试 — HP Scan Tool v4.0
覆盖: argparse 子命令解析 / 默认值 / choices 验证
不测试实际命令执行（需要网络），只测试参数解析。
"""

import pytest

import cli


# ── scan 子命令 ───────────────────────────────────────────


class TestScanArgs:
    def test_minimal(self):
        args = cli.main.__wrapped__ if hasattr(cli.main, "__wrapped__") else None
        # 直接构造 parser 来测试
        import argparse
        parser = cli._build_parser()
        args = parser.parse_args(["scan", "--ip", "192.168.1.1"])
        assert args.command == "scan"
        assert args.ip == "192.168.1.1"
        assert args.resolution == 300
        assert args.format == "jpg"
        assert args.color == "color"
        assert args.source == "Platen"
        assert args.output is None

    def test_all_options(self):
        parser = cli._build_parser()
        args = parser.parse_args([
            "scan", "--ip", "10.0.0.1",
            "--resolution", "600",
            "--format", "png",
            "--color", "gray",
            "--source", "Feeder",
            "--output", "/tmp/scans",
        ])
        assert args.ip == "10.0.0.1"
        assert args.resolution == 600
        assert args.format == "png"
        assert args.color == "gray"
        assert args.source == "Feeder"
        assert args.output == "/tmp/scans"

    def test_ip_required(self):
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan"])

    def test_invalid_format(self):
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan", "--ip", "1.2.3.4", "--format", "tiff"])

    def test_invalid_color(self):
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan", "--ip", "1.2.3.4", "--color", "cmyk"])

    def test_invalid_source(self):
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan", "--ip", "1.2.3.4", "--source", "Duplex"])


# ── discover 子命令 ──────────────────────────────────────


class TestDiscoverArgs:
    def test_defaults(self):
        parser = cli._build_parser()
        args = parser.parse_args(["discover"])
        assert args.command == "discover"
        assert args.timeout == 4.0
        assert args.format == "text"
        assert args.wsd_only is False
        assert args.mdns_only is False

    def test_json_format(self):
        parser = cli._build_parser()
        args = parser.parse_args(["discover", "--format", "json"])
        assert args.format == "json"

    def test_wsd_only(self):
        parser = cli._build_parser()
        args = parser.parse_args(["discover", "--wsd-only"])
        assert args.wsd_only is True

    def test_mdns_only(self):
        parser = cli._build_parser()
        args = parser.parse_args(["discover", "--mdns-only"])
        assert args.mdns_only is True

    def test_custom_timeout(self):
        parser = cli._build_parser()
        args = parser.parse_args(["discover", "--timeout", "10.0"])
        assert args.timeout == 10.0


# ── status 子命令 ────────────────────────────────────────


class TestStatusArgs:
    def test_defaults(self):
        parser = cli._build_parser()
        args = parser.parse_args(["status", "--ip", "192.168.1.1"])
        assert args.command == "status"
        assert args.ip == "192.168.1.1"
        assert args.format == "text"

    def test_json_format(self):
        parser = cli._build_parser()
        args = parser.parse_args(["status", "--ip", "1.2.3.4", "--format", "json"])
        assert args.format == "json"

    def test_ip_required(self):
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["status"])


# ── history 子命令 ────────────────────────────────────────


class TestHistoryArgs:
    def test_defaults(self):
        parser = cli._build_parser()
        args = parser.parse_args(["history"])
        assert args.command == "history"
        assert args.limit == 20
        assert args.format == "text"

    def test_custom_limit(self):
        parser = cli._build_parser()
        args = parser.parse_args(["history", "--limit", "50"])
        assert args.limit == 50

    def test_json_format(self):
        parser = cli._build_parser()
        args = parser.parse_args(["history", "--format", "json"])
        assert args.format == "json"


# ── 无子命令 ──────────────────────────────────────────────


class TestNoCommand:
    def test_no_args_returns_zero(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["hp_scan"])
        result = cli.main()
        assert result == 0


# ── discover 命令体 ───────────────────────────────────────


class TestDiscoverCommand:
    """真正调用 cmd_discover，覆盖三条分支。

    回归缺陷：原实现写作 `args.mdns-only`（减号），argparse dest 是
    `mdns_only`。该 elif 表达式无条件求值，导致 discover 整条命令
    抛 AttributeError，只有 --wsd-only 因走第一分支幸免。
    """

    def _args(self, *argv):
        return cli._build_parser().parse_args(list(argv))

    def test_default_uses_both_protocols(self, monkeypatch, capsys):
        called = {}

        def fake_all(timeout=4.0):
            called["all"] = timeout
            return []

        monkeypatch.setattr(cli, "discover_all_scanners", fake_all)
        assert cli.cmd_discover(self._args("discover", "--timeout", "3")) == 0
        assert called["all"] == 3.0
        assert "未发现任何扫描仪" in capsys.readouterr().out

    def test_mdns_only(self, monkeypatch, capsys):
        called = {}

        def fake_mdns(timeout=4.0):
            called["mdns"] = timeout
            return []

        monkeypatch.setattr(cli, "discover_scanners", fake_mdns)
        assert cli.cmd_discover(self._args("discover", "--mdns-only")) == 0
        assert called["mdns"] == 4.0

    def test_wsd_only(self, monkeypatch, capsys):
        called = {}

        def fake_wsd(timeout=4.0):
            called["wsd"] = timeout
            return []

        # cmd_discover 内部用 `from wsd_engine import discover_wsd`（函数内导入），
        # 因此必须打在源模块上才能真正拦截调用；cli 模块本身没有该属性。
        monkeypatch.setattr("wsd_engine.discover_wsd", fake_wsd)
        assert cli.cmd_discover(self._args("discover", "--wsd-only")) == 0
        assert called["wsd"] == 4.0

    def test_json_output_shape(self, monkeypatch, capsys):
        from escl_engine import ScannerInfo
        s = ScannerInfo(name="t", ip="1.2.3.4", model="HP M232")
        monkeypatch.setattr(cli, "discover_all_scanners", lambda timeout=4.0: [s])
        assert cli.cmd_discover(self._args("discover", "--format", "json")) == 0
        import json as _json
        # cmd_discover 先在 stdout 打印一行进度提示，再打印 JSON 数组，
        # 因此从首个 "[" 起截取 JSON 主体。
        out = capsys.readouterr().out
        data = _json.loads(out[out.index("["):])
        assert len(data) == 1 and data[0]["ip"] == "1.2.3.4"
