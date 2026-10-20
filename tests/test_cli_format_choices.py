"""
CLI 输出格式契约回归测试 — HP Scan Tool v4.0

锁死 m-6（Minor）：`--format bmp` 静默降级
  REQUEST_FORMAT 没有 bmp 键 ⇒ `hp_scan scan --format bmp` 静默回退
  ("image/jpeg", "jpg")，请求 JPEG、落盘 HP_CLI_*.jpg，用户要的 bmp 无声消失。

  修法（选 option b）：CLI 的 `--format` choices 去掉 bmp ⇒ 不支持的格式
  **显式报错**（argparse 直接退出），不再静默降级。
  协议层同样兜底：`_create_scan_job` 对不支持的格式显式抛错。
"""

import argparse

import pytest

import cli


class TestScanFormatChoices:
    def test_bmp_is_rejected_explicitly(self):
        """bmp 必须被 argparse 显式拒绝（SystemExit），不能静默降级"""
        parser = cli._build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["scan", "--ip", "1.2.3.4", "--format", "bmp"])
        assert exc.value.code != 0, "参数错误必须以非 0 退出码结束"

    def test_bmp_rejection_message_names_the_bad_value(self, capsys):
        """报错文案要说清是哪个值不合法"""
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan", "--ip", "1.2.3.4", "--format", "bmp"])
        err = capsys.readouterr().err
        assert "bmp" in err

    @pytest.mark.parametrize("fmt", ["jpg", "jpeg", "png", "pdf", "tiff", "tif"])
    def test_supported_formats_still_accepted(self, fmt):
        parser = cli._build_parser()
        args = parser.parse_args(["scan", "--ip", "1.2.3.4", "--format", fmt])
        assert args.format == fmt

    def test_default_format_is_jpg(self):
        parser = cli._build_parser()
        args = parser.parse_args(["scan", "--ip", "1.2.3.4"])
        assert args.format == "jpg"

    def test_tiff_is_accepted_now(self):
        """F-6：协议层与 GUI 都支持 tiff，CLI 之前不暴露（反向缺口）——现已放开。

        协议层 REQUEST_FORMAT["tiff"] = ("image/tiff", "tiff")，
        GUI 的格式列表也早就有 tiff ⇒ CLI 不该比它们窄。
        """
        parser = cli._build_parser()
        args = parser.parse_args(["scan", "--ip", "1.2.3.4", "--format", "tiff"])
        assert args.format == "tiff"

    def test_cli_choices_equal_request_format_keys(self):
        """CLI 的 --format 允许集合必须**逐字等于**协议层 REQUEST_FORMAT 的键集合。

        - 少一个 ⇒ 反向缺口（协议层/GUI 支持、CLI 拒绝），用户拿不到；
        - 多一个 ⇒ 静默降级风险（CLI 能选、协议层不支持）。
        """
        from escl_engine import REQUEST_FORMAT

        parser = cli._build_parser()
        sub = next(a for a in parser._actions
                   if isinstance(a, argparse._SubParsersAction)).choices["scan"]
        action = next(a for a in sub._actions if a.dest == "format")
        assert set(action.choices) == set(REQUEST_FORMAT)
        # bmp 仍然不在内（协议层不支持 ⇒ 显式拒绝）
        assert "bmp" not in action.choices


class TestEveryFormatLandsOnDisk:
    """F-6 收尾：放开格式之后，**每个**格式都要真的能走通落盘。"""

    @staticmethod
    def _jpeg_bytes(w=120, h=160):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "white").save(buf, "JPEG")
        return buf.getvalue()

    @staticmethod
    def _stub(monkeypatch, data, ext):
        monkeypatch.setattr(cli, "probe_escl",
                            lambda ip, **kw: "http://192.168.1.2/eSCL/")
        monkeypatch.setattr(cli, "fetch_capabilities", lambda s, **kw: s)
        monkeypatch.setattr(cli, "execute_scan", lambda **kw: (data, ext))

    @pytest.mark.parametrize("fmt", ["jpg", "jpeg", "png", "tiff", "tif"])
    def test_non_pdf_format_lands_with_normalized_ext(
            self, monkeypatch, tmp_path, tmp_config_dir, fmt):
        """非 PDF：按归一后的扩展名落盘，字节原样写入。"""
        from escl_engine import REQUEST_FORMAT

        data = b"\x00raw-image-bytes\x00"
        expected_ext = REQUEST_FORMAT[fmt][1]
        self._stub(monkeypatch, data, expected_ext)
        out = tmp_path / "out"

        rc = cli.cmd_scan(cli._build_parser().parse_args(
            ["scan", "--ip", "192.168.1.2", "--format", fmt, "--output", str(out)]))

        assert rc == 0
        files = list(out.glob(f"*.{expected_ext}"))
        assert len(files) == 1, f"应产出 1 个 .{expected_ext}，实际：{list(out.iterdir())}"
        assert files[0].read_bytes() == data

    def test_pdf_format_lands_as_real_pdf(self, monkeypatch, tmp_path, tmp_config_dir):
        """PDF 仍走本地合成（打印机只出图片），落盘必须是真 PDF。"""
        self._stub(monkeypatch, self._jpeg_bytes(), "pdf")
        out = tmp_path / "out"

        rc = cli.cmd_scan(cli._build_parser().parse_args(
            ["scan", "--ip", "192.168.1.2", "--format", "pdf", "--output", str(out)]))

        assert rc == 0
        pdfs = list(out.glob("*.pdf"))
        assert len(pdfs) == 1
        assert pdfs[0].read_bytes()[:5] == b"%PDF-"
