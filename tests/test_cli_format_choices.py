"""
CLI 输出格式契约回归测试 — HP Scan Tool v4.0

锁死 m-6（Minor）：`--format bmp` 静默降级
  REQUEST_FORMAT 没有 bmp 键 ⇒ `hp_scan scan --format bmp` 静默回退
  ("image/jpeg", "jpg")，请求 JPEG、落盘 HP_CLI_*.jpg，用户要的 bmp 无声消失。

  修法（选 option b）：CLI 的 `--format` choices 去掉 bmp ⇒ 不支持的格式
  **显式报错**（argparse 直接退出），不再静默降级。
  协议层同样兜底：`_create_scan_job` 对不支持的格式显式抛错。
"""

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

    @pytest.mark.parametrize("fmt", ["jpg", "png", "pdf"])
    def test_supported_formats_still_accepted(self, fmt):
        parser = cli._build_parser()
        args = parser.parse_args(["scan", "--ip", "1.2.3.4", "--format", fmt])
        assert args.format == fmt

    def test_default_format_is_jpg(self):
        parser = cli._build_parser()
        args = parser.parse_args(["scan", "--ip", "1.2.3.4"])
        assert args.format == "jpg"

    def test_tiff_still_rejected_by_cli(self):
        """CLI 原本就不支持 tiff（与 GUI 的格式列表不同），保持不变"""
        parser = cli._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["scan", "--ip", "1.2.3.4", "--format", "tiff"])
