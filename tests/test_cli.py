"""
cli.py 单元测试 — HP Scan Tool v4.0
覆盖: argparse 子命令解析 / 默认值 / choices 验证 / 命令体行为
命令体测试会真实调用 cmd_* 函数；涉及网络的扫描仪发现已打桩替换，
不产生真实网络流量。--format json 时 stdout 必须是纯 JSON。
"""

import io
import os

import pytest
from PIL import Image
from pypdf import PdfReader

import cli
import history_manager


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
        # bmp：协议层 REQUEST_FORMAT 无此键 ⇒ 必须被 argparse 显式拒绝
        # （tiff/jpeg/tif 是协议层支持的格式，已于 F-6 放开，见 test_cli_format_choices.py）
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan", "--ip", "1.2.3.4", "--format", "bmp"])

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
        """走 WSD 分支。

        覆盖强度说明：本用例**不覆盖**被修复的那行 `elif args.mdns_only:`——
        `if args.wsd_only:` 为真时短路，那个 elif 根本不求值。
        该缺陷的回归保护由 test_default_uses_both_protocols /
        test_mdns_only / test_json_output_shape 三个用例承担。
        """
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
        out = capsys.readouterr().out
        # stdout 必须是纯 JSON：进度提示已改到 stderr，首字符即数组开头
        assert out.startswith("[")
        data = _json.loads(out)
        assert len(data) == 1 and data[0]["ip"] == "1.2.3.4"

    def test_json_stdout_is_pure_json(self, monkeypatch, capsys):
        """--format json 时 stdout 只能是 JSON 数组，进度提示必须落在 stderr。

        回归缺陷：进度提示原先打印到 stdout，导致整段 stdout 不是合法 JSON，
        `cli.py discover --format json | jq` 之类的消费方式直接解析失败。
        """
        from escl_engine import ScannerInfo
        s = ScannerInfo(name="t", ip="1.2.3.4", model="HP M232")
        monkeypatch.setattr(cli, "discover_all_scanners", lambda timeout=4.0: [s])
        assert cli.cmd_discover(self._args("discover", "--format", "json")) == 0
        import json as _json
        cap = capsys.readouterr()
        # stdout 纯净：首字符即 JSON 数组开头，且不含进度文案
        assert cap.out.startswith("[")
        assert "正在发现扫描仪" not in cap.out
        # 进度提示改到 stderr，不再污染 stdout
        assert "正在发现扫描仪" in cap.err
        assert "超时=4.0s" in cap.err
        # 整段 stdout 可被直接解析
        data = _json.loads(cap.out)
        assert len(data) == 1 and data[0]["ip"] == "1.2.3.4"


# ── scan 命令体（落盘护栏）────────────────────────────────


class TestCmdScanSave:
    """
    cmd_scan 的落盘路径护栏。

    回归缺陷（批次 2 引入）：PDF 改成「打印机只出图片、PDF 本地合成」之后，
    CLI 没同步 —— 直接把打印机返回的 JPEG 字节写进 .pdf，
    产出打不开的损坏文件（CLI 打印「已保存」、退出码 0、无任何告警）。
    审计早已记录 cmd_scan 零测试覆盖，这正是该回归能溜进来的原因。
    """

    @staticmethod
    def _jpeg_bytes(w=120, h=160):
        """造一段真实 JPEG 字节（模拟打印机返回的图片数据）。"""
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "white").save(buf, "JPEG")
        return buf.getvalue()

    @staticmethod
    def _args(*argv):
        return cli._build_parser().parse_args(list(argv))

    @staticmethod
    def _stub_scan(monkeypatch, data, ext):
        """打桩网络层：execute_scan 返回指定字节与 ext（模拟打印机行为）。"""
        monkeypatch.setattr(cli, "probe_escl",
                            lambda ip, **kw: "http://192.168.1.2/eSCL")
        monkeypatch.setattr(cli, "fetch_capabilities", lambda s, **kw: s)
        monkeypatch.setattr(cli, "execute_scan", lambda **kw: (data, ext))

    def test_pdf_output_is_real_pdf(self, monkeypatch, tmp_path, tmp_config_dir):
        """选 PDF：产出必须能被 pypdf 打开，且真的是 PDF 字节（不是 JPEG 套 .pdf）。"""
        data = self._jpeg_bytes()
        self._stub_scan(monkeypatch, data, "pdf")
        out = tmp_path / "out"

        rc = cli.cmd_scan(self._args("scan", "--ip", "192.168.1.2",
                                     "--format", "pdf", "--output", str(out)))
        assert rc == 0

        pdfs = list(out.glob("*.pdf"))
        assert len(pdfs) == 1, f"应产出 1 个 PDF，实际：{pdfs}"
        raw = pdfs[0].read_bytes()
        assert raw[:5] == b"%PDF-", "落盘产物必须是真正的 PDF 字节"
        assert raw[:3] != b"\xff\xd8", "不能是 JPEG 字节套着 .pdf 扩展名"

        reader = PdfReader(str(pdfs[0]))
        assert len(reader.pages) >= 1

    def test_jpg_written_verbatim(self, monkeypatch, tmp_path, tmp_config_dir):
        """非 PDF：字节原样写入、扩展名正确（行为与改造前完全一致）。"""
        data = self._jpeg_bytes()
        self._stub_scan(monkeypatch, data, "jpg")
        out = tmp_path / "out"

        rc = cli.cmd_scan(self._args("scan", "--ip", "192.168.1.2",
                                     "--format", "jpg", "--output", str(out)))
        assert rc == 0

        jpgs = list(out.glob("*.jpg"))
        assert len(jpgs) == 1, f"应产出 1 个 JPG，实际：{jpgs}"
        assert jpgs[0].read_bytes() == data, "非 PDF 必须原样写字节"

    def test_pdf_scan_records_history_once(self, monkeypatch, tmp_path, tmp_config_dir):
        """PDF 路径的历史只记一条：save_manager 已代记，CLI 不重复记。"""
        self._stub_scan(monkeypatch, self._jpeg_bytes(), "pdf")
        out = tmp_path / "out"

        rc = cli.cmd_scan(self._args("scan", "--ip", "192.168.1.2",
                                     "--format", "pdf", "--output", str(out)))
        assert rc == 0

        records = history_manager.list_records(limit=10)
        assert len(records) == 1, f"一次扫描只应记一条历史，实际 {len(records)} 条"
        assert records[0]["format"] == "pdf"

    def test_no_temp_files_left_behind(self, monkeypatch, tmp_path, tmp_config_dir):
        """临时缓存页与 PDF 合成的中间产物都必须清理干净。"""
        created = []

        def fake_mkdtemp(prefix=None, dir=None):
            d = tmp_path / f"tmp_{len(created)}"
            d.mkdir()
            created.append(str(d))
            return str(d)

        # cli 与 save_manager 都用 tempfile.mkdtemp，一并接管
        monkeypatch.setattr("tempfile.mkdtemp", fake_mkdtemp)
        self._stub_scan(monkeypatch, self._jpeg_bytes(), "pdf")
        out = tmp_path / "out"

        rc = cli.cmd_scan(self._args("scan", "--ip", "192.168.1.2",
                                     "--format", "pdf", "--output", str(out)))
        assert rc == 0
        assert created, "应至少创建过一个临时目录"
        leftover = [p for p in created if os.path.exists(p)]
        assert leftover == [], f"临时目录未清理：{leftover}"


class TestOutputFilenamePrecision:
    """
    ★ 回归（复审 I1）：CLI 的产物名必须到**微秒**。

    ★ 关键事实：CLI 走的是 `escl_engine.execute_scan`，**根本不经过协调器**
      ⇒ 协调器那次「秒 → 毫秒」的改动**覆盖不到 CLI 这条链**。
      CLI 之前只到秒 ⇒ 同一秒内完成的两批算出**同一个文件名**
      ⇒ 后写的把先写的整个覆盖掉 ⇒ 先写的那批产物**静默消失**
      （用户看不到，也没有任何报错）。

    ★ 2026-10-21 订正：**只到微秒仍然不够** ——
      本机系统时钟粒度 15.625 ms ⇒ 同一 tick 内两次取名的 `%f` 99.5% 相同
      ⇒ 契约扩成 `YYYYMMDD_HHMMSS_ffffff_<6 位随机>`（随机成分不依赖时钟）。
      本类下面那条**精确形状**断言（"时间戳正好 22 字符、三段"）随之改成
      **四段**（时间戳三段 + 唯一成分一段）—— 意图不变：微秒段必须还在、
      且必须还是 6 位数字。
    """

    def test_filename_carries_microseconds(self):
        name = cli._timestamped_name("HP_CLI", "jpg")
        assert name.startswith("HP_CLI_") and name.endswith(".jpg")
        ts = name[len("HP_CLI_"):-len(".jpg")]
        # 期望格式：YYYYMMDD_HHMMSS_ffffff_<6 位随机唯一成分>
        parts = ts.split("_")
        assert len(parts) == 4, (
            "应为「日期_时分秒_微秒_唯一成分」四段，实际 %r" % (ts,))
        date, clock, micro, token = parts
        assert date.isdigit() and len(date) == 8, date
        assert clock.isdigit() and len(clock) == 6, clock
        assert micro.isdigit() and len(micro) == 6, micro
        assert len(token) == 6 and all(c in "0123456789abcdef" for c in token[1:]), (
            "唯一成分必须是「1 位字母 + 5 位十六进制」，实际 %r" % (token,))
        assert token[0] in "abcdefghijklmnop", (
            "唯一成分首字符必须是字母 ⇒ 不会被误读成组号/页号，实际 %r" % (token,))

    def test_two_batches_in_the_same_second_get_different_names(self, monkeypatch):
        """★ 关键行为断言：**同一秒内**的两批 ⇒ 名字必须不同。

        把时间**冻结在同一秒**（只让微秒推进）⇒ 改回秒精度时两个名字会
        完全相同 ⇒ 这条断言会红（不是"什么都没发生也能过"的空转断言）。
        """
        from datetime import datetime as _dt

        class _FakeDT:
            _n = 0

            @classmethod
            def now(cls, tz=None):
                cls._n += 1
                return _dt(2026, 10, 21, 4, 52, 13, cls._n)

        monkeypatch.setattr(cli, "datetime", _FakeDT)
        a = cli._timestamped_name("HP_CLI", "jpg")
        b = cli._timestamped_name("HP_CLI", "jpg")
        assert a != b, f"同一秒内两批产物同名 ⇒ 会互相覆盖: {a} / {b}"
        same_second = "HP_CLI_20261021_045213"
        assert a.startswith(same_second) and b.startswith(same_second), (
            "两批应落在同一秒内，否则这条断言没测到东西: %s / %s" % (a, b))

