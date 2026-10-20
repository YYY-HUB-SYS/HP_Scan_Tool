"""
WIA 输出格式契约回归测试 — HP Scan Tool v4.0

锁死 m-5（Minor，既有缺陷但与新契约冲突）：
  新契约「ext=pdf ⇒ 字节是**图片**，PDF 由 save_manager 本地合成」。
  旧实现 `_save_wia_image(img, "pdf")`：
    - WIA 不支持 .pdf 扩展名 ⇒ SaveFile("*.pdf") 失败
    - 走 FileData 兜底 ⇒ `pil_img.save(tmp_path)` 让 PIL **按 .pdf 扩展名**
      写出**真 PDF**字节
    - 实测该字节流 Image.open 抛 UnidentifiedImageError
      ⇒ 缓存页 page_001.pdf 打不开，GUI 预览无法渲染。
  修法：pdf 内部按 jpg 处理（WIA 支持 jpg），保证交出去的是图片字节。
"""

import io

import pytest
from PIL import Image

import wia_engine


def _jpeg_bytes(width=40, height=30):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "JPEG")
    return buf.getvalue()


class _FakeFileDataResult:
    def __init__(self, binary):
        self.BinaryData = binary


class _FakeFileData:
    def __init__(self, binary):
        self._binary = binary

    def GetFile(self, FormatID=None):
        return _FakeFileDataResult(self._binary)


class _FakeWiaImage:
    """假 COM 对象：SaveFile 可选失败（模拟 WIA 不支持该扩展名）"""

    def __init__(self, jpeg_bytes, save_fails=True):
        self.FileData = _FakeFileData(jpeg_bytes)
        self._jpeg = jpeg_bytes
        self._save_fails = save_fails
        self.saved_paths = []

    def SaveFile(self, path):
        self.saved_paths.append(path)
        if self._save_fails:
            raise RuntimeError("WIA 不支持该扩展名")
        with open(path, "wb") as f:
            f.write(self._jpeg)


class _FakeItem:
    def __init__(self, img):
        self._img = img
        self.Properties = []

    def Transfer(self):
        return self._img


class _FakeDevice:
    def __init__(self, img):
        self.Items = [_FakeItem(img)]


class TestPdfBytesAreImageBytes:
    def test_file_data_fallback_returns_openable_image(self):
        """SaveFile 失败 → FileData 兜底：返回的字节必须能被 PIL 打开"""
        jpeg = _jpeg_bytes()
        img = _FakeWiaImage(jpeg, save_fails=True)

        data = wia_engine._save_wia_image(img, "pdf")

        assert data[:5] != b"%PDF-", "绝不能交出真 PDF 字节"
        opened = Image.open(io.BytesIO(data))
        assert opened.format == "JPEG", f"应当是图片字节，实际格式 {opened.format}"

    def test_file_data_fallback_uses_jpg_temp_file(self):
        """内部按 jpg 处理（临时文件后缀必须是 .jpg，PIL 才会写 JPEG）"""
        img = _FakeWiaImage(_jpeg_bytes(), save_fails=True)

        wia_engine._save_wia_image(img, "pdf")

        assert img.saved_paths, "应当调用过 SaveFile"
        assert img.saved_paths[0].endswith(".jpg"), \
            f"临时文件后缀应当是 .jpg，实际 {img.saved_paths[0]}"

    def test_savefile_success_path_returns_image_bytes(self):
        """SaveFile 成功分支：同样必须是图片字节"""
        jpeg = _jpeg_bytes()
        img = _FakeWiaImage(jpeg, save_fails=False)

        data = wia_engine._save_wia_image(img, "pdf")

        assert data == jpeg
        assert Image.open(io.BytesIO(data)).format == "JPEG"

    def test_wia_scan_pdf_returns_image_bytes(self):
        """wia_scan 层面：ext="pdf" 时字节必须是图片（PDF 由 save_manager 合成）"""
        jpeg = _jpeg_bytes()
        img = _FakeWiaImage(jpeg, save_fails=True)

        import wia_engine as we
        original = we._connect_device
        we._connect_device = lambda device_id: _FakeDevice(img)
        try:
            data, ext = we.wia_scan(device_id="x", output_format="pdf")
        finally:
            we._connect_device = original

        assert ext == "pdf"
        assert data[:5] != b"%PDF-"
        assert Image.open(io.BytesIO(data)).format == "JPEG"

    def test_jpg_path_still_works(self):
        """jpg 路径不受影响"""
        jpeg = _jpeg_bytes()
        img = _FakeWiaImage(jpeg, save_fails=True)

        data = wia_engine._save_wia_image(img, "jpg")

        assert Image.open(io.BytesIO(data)).format == "JPEG"
        assert img.saved_paths[0].endswith(".jpg")


class TestFormatGuidMap:
    def test_pdf_is_not_a_wia_format(self):
        """WIA 没有 pdf 格式 GUID（这正是旧缺陷的根因）"""
        assert "pdf" not in wia_engine._WIA_FORMAT_GUID

    def test_supported_formats_unchanged(self):
        for ext in ("jpg", "jpeg", "png", "tiff", "bmp"):
            assert ext in wia_engine._WIA_FORMAT_GUID
