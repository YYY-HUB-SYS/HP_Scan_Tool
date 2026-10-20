"""
JPEG 头"修复"逻辑的删除回归测试 — HP Scan Tool v4.0

锁死 F-3（Minor）：`escl_engine._fix_jpeg_header` 是**永不生效的死代码**
------------------------------------------------------------------
它的判定条件是"SOF 段里的高度 != PIL 报的高度"，但 PIL 的 `Image.height`
**本身就是从同一处 SOF 读出来的** ⇒ 两个值恒等 ⇒ 条件恒假 ⇒ 从未修补过
任何字节（宣称的"修复 HP 机型 JPEG 头高度元数据错误"实际不发生）。

实测（2026-10-21，删除前）：把 SOF 高度改成 900 后
  - PIL 报告 height=900（不是原图的 30）
  - `_fix_jpeg_header(bad) == bad`（逐字节相同，changed_bytes=false）

删除它同时消除了一个**隐性分叉**：`escl_engine` 的两条路径调它、
`listen_engine` 的两条路径**不调**它 —— 今天两者行为等价只是因为它是
no-op；一旦有人把它"修好"，两条路径就会分叉。

本文件锁死删除后的**确定行为**：
  - 输入 JPEG 头正常   → 引擎**原样**返回字节（不做任何改写）；
  - 输入 JPEG 头异常   → 引擎同样**原样**返回字节（不再假装能修），
    坏头由上游（PIL 解码）如实报错，不被引擎悄悄篡改。
"""

import io
import struct
from unittest import mock

import pytest
from PIL import Image

import escl_engine


def _jpeg_bytes(width=40, height=30):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "JPEG")
    return buf.getvalue()


def _sof_offset(data: bytes) -> int:
    """定位第一个 SOF0/SOF2 段（高度字段在 pos+5..pos+7）。"""
    pos = 2
    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xC0, 0xC2):
            return pos
        if pos + 3 < len(data):
            pos += 2 + struct.unpack(">H", data[pos + 2:pos + 4])[0]
        else:
            break
    raise AssertionError("没找到 SOF 段")


def _tamper_height(data: bytes, height: int) -> bytes:
    """把 SOF 里的高度改成指定值（模拟"头高度写错"的坏 JPEG）。"""
    pos = _sof_offset(data)
    bad = bytearray(data)
    bad[pos + 5:pos + 7] = struct.pack(">H", height)
    return bytes(bad)


class _Resp:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = ""
        self.headers = headers or {}


def _run_execute_scan(monkeypatch, content: bytes):
    """跑一次真实的 execute_scan（只桩网络层），返回引擎交出去的字节。"""
    scanner = escl_engine.ScannerInfo(name="t", ip="1.2.3.4")
    scanner.escl_url = "http://1.2.3.4:80/eSCL/"

    session = mock.MagicMock()
    session.post.return_value = _Resp(
        201, b"", {"Location": "http://1.2.3.4:80/eSCL/ScanJobs/1"})
    session.get.return_value = _Resp(200, content)

    with mock.patch.object(escl_engine.requests, "Session", return_value=session):
        data, ext = escl_engine.execute_scan(scanner=scanner, output_format="jpg")
    return data, ext


class TestJpegHeaderRepairRemoved:
    def test_function_is_gone(self):
        """死代码已删除（避免有人照着注释再实现一遍同样的恒等比较）。"""
        assert not hasattr(escl_engine, "_fix_jpeg_header")

    def test_no_call_site_remains(self):
        """整个项目里不再有 _fix_jpeg_header 的**代码引用**。

        只看代码行（跳过整行注释）：escl_engine.py 里保留了一段注释说明
        "为什么删掉它"，那是文档不是调用点。
        """
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        this_file = os.path.basename(__file__)
        hits = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", ".venv-local", "build", "dist",
                                        "__pycache__", ".superpowers")]
            for name in filenames:
                if not name.endswith(".py") or name == this_file:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        code = [ln for ln in f.read().splitlines()
                                if not ln.lstrip().startswith("#")]
                except (OSError, UnicodeDecodeError):
                    continue
                if any("_fix_jpeg_header" in ln for ln in code):
                    hits.append(path)
        assert hits == [], f"仍有 _fix_jpeg_header 代码引用：{hits}"

    def test_normal_jpeg_passes_through_verbatim(self, monkeypatch):
        """输入 JPEG 头正常 ⇒ 引擎**原样**返回字节（不转码、不改写）。"""
        data = _jpeg_bytes()
        out, ext = _run_execute_scan(monkeypatch, data)

        assert ext == "jpg"
        assert out == data, "正常 JPEG 必须逐字节原样返回"
        assert Image.open(io.BytesIO(out)).size == (40, 30)

    def test_broken_jpeg_header_is_not_silently_rewritten(self, monkeypatch):
        """输入 JPEG 头异常（SOF 高度被改坏）⇒ 引擎**原样**返回，不假装能修。

        这条锁死的是删除后的确定行为：引擎不再对字节做任何"修复"，
        坏头由上游（如 PIL 解码）如实报错，而不是被悄悄篡改成
        "看起来能用但内容错乱"的图。
        """
        good = _jpeg_bytes()
        bad = _tamper_height(good, 900)

        # 先自证：坏头的 PIL 高度确实变了（说明这份数据确实是"头坏了"）
        assert Image.open(io.BytesIO(bad)).size == (40, 900)

        out, ext = _run_execute_scan(monkeypatch, bad)

        assert ext == "jpg"
        assert out == bad, "引擎不得再对 JPEG 字节做任何改写"
