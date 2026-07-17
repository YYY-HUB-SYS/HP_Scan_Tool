"""
cli.py 单元测试 — HP Scan Tool v3.3
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
        assert args.exposure == "off"
        assert args.output is None

    def test_all_options(self):
        parser = cli._build_parser()
        args = parser.parse_args([
            "scan", "--ip", "10.0.0.1",
            "--resolution", "600",
            "--format", "png",
            "--color", "gray",
            "--source", "Feeder",
            "--exposure", "auto",
            "--output", "/tmp/scans",
        ])
        assert args.ip == "10.0.0.1"
        assert args.resolution == 600
        assert args.format == "png"
        assert args.color == "gray"
        assert args.source == "Feeder"
        assert args.exposure == "auto"
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

    def test_invalid_exposure(self):
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan", "--ip", "1.2.3.4", "--exposure", "vivid"])


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
