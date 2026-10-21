# -*- coding: utf-8 -*-
"""
CLI `--exposure` 曝光预设参数 — HP Scan Tool v4.0

被守护的行为
------------------------------------------------------------------
① 不带 `--exposure` ⇒ **行为与改造前完全一致**（jpg 字节原样落盘、
   历史记录不多出任何字段、参数默认值是 None）。
② 带**合法内置预设**（文字增强 / 照片还原 / 去灰底 / 旧文件修复）⇒
   曝光真的落到像素上（产物与不加曝光的产物**可测量地不同**）。
③ 带**合法用户自定义预设**（%APPDATA%\\HP_Scan_Tool\\presets.json）⇒
   同样生效。
④ 预设**不存在** ⇒ **非零退出码** + **中文提示**（列出可用预设名）；
   stdout 保持为空、stderr 不带 Python traceback、**不发起任何扫描**。
⑤ `--help` 里能看到 `--exposure` 与内置预设名单。

★ 用户数据零污染
   - 进程内用例：走 tests/conftest.py 的 APPDATA 沙箱（`_isolate_user_data`）
     + `tmp_config_dir`（把 presets.json / history.json 重定向到 tmp_path）；
   - 子进程用例：**自己**把 APPDATA 指到 tmp_path 沙箱。

★ 不发任何真实网络请求
   - 进程内用例：网络层（probe_escl / fetch_capabilities / execute_scan）
     全部 monkeypatch 掉；
   - 子进程用例：只跑 `--help` 与「预设不存在」——后者在**任何网络动作之前**
     就退出 ⇒ 零流量。

★ 不弹任何窗口
   - 全文件不触碰 os.startfile / explorer。
"""

import io
import json
import os
import pathlib
import subprocess
import sys

import pytest
from PIL import Image

import cli
import preset_manager

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 灰底图的基准灰度（中间调 ⇒ 曝光的明/暗变化最容易被测出来）
BASE_GRAY = 128

#: 内置预设「文字增强」：manual / brightness 15 / contrast 40 / gamma 0.8
#: ⇒ 中间调会被压**暗**（实测 128 → 114）
BUILTIN_DARKEN = "文字增强"

#: 用户自定义预设（测试自己写进沙箱 presets.json）
USER_PRESET_NAME = "测试提亮"
USER_PRESET = {
    "name": USER_PRESET_NAME,
    "mode": "manual",
    "brightness": 60, "contrast": 0,
    "gamma": 1.0, "shadows": 0, "highlights": 0,
    "channel_gains": [1.0, 1.0, 1.0],
}


# ── 小工具 ────────────────────────────────────────────────────────────


def _args(*argv):
    """用真实 parser 造 args（走 argparse 的校验路径）。"""
    return cli._build_parser().parse_args(list(argv))


def _gray_jpeg_bytes(value=BASE_GRAY, w=120, h=160):
    """造一段真实 JPEG 字节（模拟打印机返回的中间调图片）。"""
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (value, value, value)).save(buf, "JPEG")
    return buf.getvalue()


def _mean_luma(data: bytes) -> float:
    """一段图片字节的平均亮度（0~255）。"""
    with Image.open(io.BytesIO(data)) as im:
        raw = im.convert("L").tobytes()
    return sum(raw) / len(raw)


def _stub_scan(monkeypatch, data, ext):
    """打桩网络层：execute_scan 返回指定字节与 ext（模拟打印机行为）。"""
    monkeypatch.setattr(cli, "probe_escl",
                        lambda ip, **kw: "http://192.168.1.2/eSCL")
    monkeypatch.setattr(cli, "fetch_capabilities", lambda s, **kw: s)
    monkeypatch.setattr(cli, "execute_scan", lambda **kw: (data, ext))


def _write_user_presets(config_dir, presets):
    """把用户自定义预设写进沙箱的 presets.json（%APPDATA% 的替身）。"""
    path = pathlib.Path(config_dir) / "presets.json"
    path.write_text(json.dumps(presets, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return str(path)


def _run_cli(argv, appdata, timeout=120):
    """拉起真实 CLI 子进程，返回 CompletedProcess（stdout/stderr 是原始字节）。"""
    env = os.environ.copy()
    env["APPDATA"] = str(appdata)              # 用户数据隔离
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"           # 中文提示按 UTF-8 解码
    return subprocess.run(
        [sys.executable, "-m", "cli"] + list(argv),
        cwd=str(PROJECT_ROOT), env=env,
        capture_output=True, timeout=timeout,
    )


# ══════════════════════════════════════════════════════════════════════
# ① 不带 --exposure ⇒ 行为与改造前完全一致
# ══════════════════════════════════════════════════════════════════════


class TestNoExposureIsUnchanged:
    """★ 向后兼容护栏：不带 --exposure 时**不许有任何行为变化**。"""

    def test_default_value_is_none(self):
        """参数默认值必须是 None（= 不做曝光）。"""
        args = _args("scan", "--ip", "192.168.1.2")
        assert args.exposure is None

    def test_jpg_bytes_are_verbatim_without_exposure(self, monkeypatch, tmp_path,
                                                     tmp_config_dir):
        """非 PDF：字节必须**原样**落盘（与改造前逐字节相同）。"""
        data = _gray_jpeg_bytes()
        _stub_scan(monkeypatch, data, "jpg")
        out = tmp_path / "out"

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                 "--format", "jpg", "--output", str(out)))

        assert rc == 0
        jpgs = list(out.glob("*.jpg"))
        assert len(jpgs) == 1, "应产出 1 个 JPG，实际：%r" % (jpgs,)
        assert jpgs[0].read_bytes() == data, "不带 --exposure 时字节必须原样落盘"

    def test_history_record_has_no_exposure_key(self, monkeypatch, tmp_path,
                                                 tmp_config_dir):
        """历史记录不许多出 "exposure" 字段（否则历史内容就变了）。"""
        import history_manager

        _stub_scan(monkeypatch, _gray_jpeg_bytes(), "jpg")
        out = tmp_path / "out"

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                 "--output", str(out)))

        assert rc == 0
        records = history_manager.list_records(limit=10)
        assert len(records) == 1
        assert "exposure" not in records[0], (
            "不带 --exposure 时历史记录不许出现 exposure 字段：%r" % records[0])

    def test_progress_line_unchanged_without_exposure(self, monkeypatch, capsys,
                                                      tmp_path, tmp_config_dir):
        """进度提示的文案也不许变（不带曝光时不提"曝光"两个字）。"""
        _stub_scan(monkeypatch, _gray_jpeg_bytes(), "jpg")

        cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                           "--output", str(tmp_path / "out")))

        err = capsys.readouterr().err
        assert "正在扫描 (DPI=300, 格式=jpg, 来源=Platen)..." in err, err


# ══════════════════════════════════════════════════════════════════════
# ② 合法内置预设 ⇒ 生效
# ══════════════════════════════════════════════════════════════════════


class TestBuiltinPresetApplies:
    """★ 内置预设必须真的落到像素上（可测量地不同）。"""

    def test_builtin_preset_is_accepted(self):
        args = _args("scan", "--ip", "192.168.1.2", "--exposure", BUILTIN_DARKEN)
        assert args.exposure == BUILTIN_DARKEN

    def test_builtin_preset_darkens_the_output(self, monkeypatch, tmp_path,
                                                tmp_config_dir):
        """「文字增强」把中间调压暗 ⇒ 产物亮度必须明显低于不加曝光时。"""
        data = _gray_jpeg_bytes()
        _stub_scan(monkeypatch, data, "jpg")
        plain_dir = tmp_path / "plain"
        exposed_dir = tmp_path / "exposed"

        assert cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                  "--output", str(plain_dir))) == 0
        assert cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                  "--exposure", BUILTIN_DARKEN,
                                  "--output", str(exposed_dir))) == 0

        plain = list(plain_dir.glob("*.jpg"))[0].read_bytes()
        exposed = list(exposed_dir.glob("*.jpg"))[0].read_bytes()

        assert exposed != plain, "加了曝光却产出完全相同的字节 ⇒ 曝光没生效"
        assert _mean_luma(exposed) < _mean_luma(plain) - 8, (
            "「文字增强」应把中间调压暗：plain=%.1f exposed=%.1f"
            % (_mean_luma(plain), _mean_luma(exposed)))

    def test_builtin_preset_keeps_geometry(self, monkeypatch, tmp_path,
                                            tmp_config_dir):
        """★ 曝光只改像素、**不改几何**（不缩放、不加 A4 白边）。"""
        data = _gray_jpeg_bytes(w=120, h=160)
        _stub_scan(monkeypatch, data, "jpg")
        out = tmp_path / "out"

        assert cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                  "--exposure", BUILTIN_DARKEN,
                                  "--output", str(out))) == 0

        jpg = list(out.glob("*.jpg"))[0]
        with Image.open(jpg) as im:
            assert im.size == (120, 160), "曝光不许改变图片尺寸：%r" % (im.size,)

    def test_exposure_dict_reaches_save_manager_for_pdf(self, monkeypatch,
                                                        tmp_path,
                                                        tmp_config_dir):
        """PDF 路径：曝光参数字典必须**原样**交给 save_manager（GUI 同一份链路）。"""
        captured = {}

        def fake_save_single(cache_path, ext, output_path, **kwargs):
            captured["ext"] = ext
            captured["exposure"] = kwargs.get("exposure")
            with open(output_path, "wb") as f:
                f.write(b"%PDF-1.4\n")
            return output_path

        _stub_scan(monkeypatch, _gray_jpeg_bytes(), "pdf")
        monkeypatch.setattr(cli.save_manager, "save_single", fake_save_single)

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                 "--format", "pdf", "--exposure", BUILTIN_DARKEN,
                                 "--output", str(tmp_path / "out")))

        assert rc == 0
        assert captured["ext"] == "pdf"
        assert captured["exposure"], "save_manager 必须收到曝光参数字典"
        assert captured["exposure"]["mode"] == "manual"
        assert captured["exposure"]["brightness"] == 15
        assert captured["exposure"]["contrast"] == 40

    def test_pdf_with_exposure_is_a_real_pdf(self, monkeypatch, tmp_path,
                                              tmp_config_dir):
        """PDF + 曝光：产物必须仍是**真 PDF**（不是 JPEG 套 .pdf）。"""
        _stub_scan(monkeypatch, _gray_jpeg_bytes(), "pdf")
        out = tmp_path / "out"

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                 "--format", "pdf", "--exposure", BUILTIN_DARKEN,
                                 "--output", str(out)))

        assert rc == 0
        pdfs = list(out.glob("*.pdf"))
        assert len(pdfs) == 1
        raw = pdfs[0].read_bytes()
        assert raw[:5] == b"%PDF-", "落盘产物必须是真正的 PDF 字节"
        assert raw[:3] != b"\xff\xd8", "不能是 JPEG 字节套着 .pdf 扩展名"

    def test_pdf_with_exposure_has_darker_page_than_plain(self, monkeypatch,
                                                          tmp_path,
                                                          tmp_config_dir):
        """PDF 页面里的图片也必须被曝光处理过（比不加曝光时更暗）。"""
        from pypdf import PdfReader

        def _page_center_mean(pdf_path, box=(120, 160)):
            """取 PDF 页面**中心**那块（= 贴进 A4 画布的源图）的平均亮度。

            源图比 A4 画布小 ⇒ save_manager 是**原尺寸居中贴**（不放大），
            所以页面正中间 120×160 那一块就是源图本身。
            """
            reader = PdfReader(str(pdf_path))
            images = reader.pages[0].images
            assert images, "PDF 页面里应嵌有图片"
            with Image.open(io.BytesIO(images[0].data)) as page_img:
                w, h = page_img.size
                bw, bh = box
                cx, cy = w // 2, h // 2
                region = page_img.convert("L").crop(
                    (cx - bw // 2, cy - bh // 2,
                     cx - bw // 2 + bw, cy - bh // 2 + bh))
                raw = region.tobytes()
            return sum(raw) / len(raw)

        _stub_scan(monkeypatch, _gray_jpeg_bytes(), "pdf")
        plain_dir = tmp_path / "plain"
        exposed_dir = tmp_path / "exposed"

        assert cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                  "--format", "pdf",
                                  "--output", str(plain_dir))) == 0
        assert cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                  "--format", "pdf", "--exposure", BUILTIN_DARKEN,
                                  "--output", str(exposed_dir))) == 0

        plain = list(plain_dir.glob("*.pdf"))[0]
        exposed = list(exposed_dir.glob("*.pdf"))[0]
        assert _page_center_mean(exposed) < _page_center_mean(plain) - 8, (
            "PDF 页面里的图片应被曝光压暗：plain=%.1f exposed=%.1f"
            % (_page_center_mean(plain), _page_center_mean(exposed)))


# ══════════════════════════════════════════════════════════════════════
# ③ 合法用户自定义预设 ⇒ 生效
# ══════════════════════════════════════════════════════════════════════


class TestUserPresetApplies:
    """★ 用户自定义预设（presets.json）必须与内置预设同等对待。"""

    def test_user_preset_brightens_the_output(self, monkeypatch, tmp_path,
                                              tmp_config_dir):
        _write_user_presets(tmp_config_dir, [USER_PRESET])
        data = _gray_jpeg_bytes()
        _stub_scan(monkeypatch, data, "jpg")
        plain_dir = tmp_path / "plain"
        exposed_dir = tmp_path / "exposed"

        assert cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                  "--output", str(plain_dir))) == 0
        assert cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                  "--exposure", USER_PRESET_NAME,
                                  "--output", str(exposed_dir))) == 0

        plain = list(plain_dir.glob("*.jpg"))[0].read_bytes()
        exposed = list(exposed_dir.glob("*.jpg"))[0].read_bytes()

        assert exposed != plain, "加了自定义预设却产出相同字节 ⇒ 预设没生效"
        assert _mean_luma(exposed) > _mean_luma(plain) + 40, (
            "「%s」应把中间调提亮：plain=%.1f exposed=%.1f"
            % (USER_PRESET_NAME, _mean_luma(plain), _mean_luma(exposed)))

    def test_user_preset_is_loaded_from_presets_json(self, tmp_config_dir):
        """预设确实来自 %APPDATA%\\HP_Scan_Tool\\presets.json（沙箱替身）。"""
        _write_user_presets(tmp_config_dir, [USER_PRESET])
        names = [p.get("name") for p in preset_manager.load_presets()]
        assert USER_PRESET_NAME in names
        assert BUILTIN_DARKEN in names, "内置预设必须同时可用"

    def test_history_records_the_applied_exposure(self, monkeypatch, tmp_path,
                                                  tmp_config_dir):
        """用了曝光 ⇒ 历史记录里应能看出曝光参数（与 save_manager 同口径）。"""
        import history_manager

        _write_user_presets(tmp_config_dir, [USER_PRESET])
        _stub_scan(monkeypatch, _gray_jpeg_bytes(), "jpg")

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                 "--exposure", USER_PRESET_NAME,
                                 "--output", str(tmp_path / "out")))

        assert rc == 0
        records = history_manager.list_records(limit=10)
        assert len(records) == 1
        assert records[0]["exposure"]["brightness"] == 60


# ══════════════════════════════════════════════════════════════════════
# ④ 预设不存在 ⇒ 非零退出码 + 中文提示
# ══════════════════════════════════════════════════════════════════════


class TestUnknownPresetIsRejected:
    """★ 不许静默忽略、不许吐 Python traceback。"""

    def test_unknown_preset_returns_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "probe_escl",
                            lambda ip, **kw: pytest.fail("不该发起网络探测"))
        monkeypatch.setattr(cli, "execute_scan",
                            lambda **kw: pytest.fail("不该发起扫描"))

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                 "--exposure", "不存在的预设"))

        assert rc != 0, "预设不存在必须返回非 0 退出码"
        captured = capsys.readouterr()
        assert captured.out == "", "失败时 stdout 必须为空，实际 %r" % captured.out
        err = captured.err
        assert "曝光预设" in err, "stderr 必须点名「曝光预设」：%r" % err
        assert "不存在" in err, "stderr 必须说清「不存在」：%r" % err
        assert "不存在的预设" in err, "stderr 必须回显用户填的名字：%r" % err
        assert BUILTIN_DARKEN in err, "stderr 必须列出可用预设名：%r" % err
        assert "Traceback" not in err, "stderr 不许带 Python traceback：%r" % err

    def test_unknown_preset_writes_no_output_file(self, monkeypatch, tmp_path,
                                                  tmp_config_dir):
        """预设不存在 ⇒ 不扫描、不落盘。"""
        _stub_scan(monkeypatch, _gray_jpeg_bytes(), "jpg")
        out = tmp_path / "out"

        rc = cli.cmd_scan(_args("scan", "--ip", "192.168.1.2",
                                 "--exposure", "不存在的预设",
                                 "--output", str(out)))

        assert rc != 0
        assert not out.exists() or not list(out.glob("*")), "失败时不该落盘"

    def test_unknown_preset_subprocess_exit_code(self, tmp_path):
        """★ 子进程级：真实 CLI 进程退出码非 0、stdout 干净、stderr 是中文提示。"""
        appdata = tmp_path / "appdata"
        appdata.mkdir(parents=True, exist_ok=True)

        proc = _run_cli(["scan", "--ip", "192.168.1.2",
                         "--exposure", "不存在的预设"], appdata)

        assert proc.returncode != 0, "预设不存在必须以非 0 退出码结束"
        assert proc.stdout == b"", "失败时 stdout 必须为空，实际 %r" % proc.stdout[:200]
        err = proc.stderr.decode("utf-8", errors="replace")
        assert "曝光预设" in err, "stderr 必须点名「曝光预设」：%r" % err
        assert BUILTIN_DARKEN in err, "stderr 必须列出可用预设名：%r" % err
        assert "Traceback" not in err, "真实进程的 stderr 不许带 traceback：%r" % err
        assert "  File \"" not in err, "不该把源码路径甩给用户：%r" % err


# ══════════════════════════════════════════════════════════════════════
# ⑤ --help 里能看到 --exposure
# ══════════════════════════════════════════════════════════════════════


class TestHelpDocumentsExposure:
    """★ --help 必须把参数与内置预设名单讲清楚。"""

    def test_scan_help_shows_exposure_option(self, tmp_path):
        appdata = tmp_path / "appdata"
        appdata.mkdir(parents=True, exist_ok=True)

        proc = _run_cli(["scan", "--help"], appdata)

        assert proc.returncode == 0, (
            "退出码 %d，stderr=%r" % (proc.returncode, proc.stderr[:2000]))
        out = proc.stdout.decode("utf-8", errors="replace")
        assert "--exposure" in out, "--help 里必须出现 --exposure：%r" % out
        # ★ 名单取自 preset_manager（预设名的**唯一真源**）⇒ help 不会与 GUI 漂移
        for preset in preset_manager.builtin_presets():
            assert preset["name"] in out, (
                "--help 里必须列出内置预设 %r：%r" % (preset["name"], out))
        assert "presets.json" in out, (
            "--help 必须指向用户自定义预设文件：%r" % out)
