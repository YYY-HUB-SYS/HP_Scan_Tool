"""
**逐页曝光覆盖**（多页预览里每一页可单独调曝光）—— 落盘层回归测试

★ 背景
---------------------------------------------------------------------------
曝光此前是**文档级**参数：`save_pages(..., exposure=...)` 一份值套到**所有**页。
「多页预览的逐页曝光覆盖」要求：**某一页可以单独用自己的曝光**，其余页跟随整批。

★ 本层契约（GUI 侧照此接线）
  - `save_pages(..., page_exposures=None)`：`page_exposures` 与 **`page_specs` 等长**、
    **下标对齐**；元素为 `None` ⇒ 该页**跟随整批**的 `exposure`；
    元素为 dict ⇒ 该页**单独**用自己的曝光。
  - `SaveRequest.page_exposures` + `run_save_job` 原样透传。
  - 覆盖值走**同一条**净化路径（`_exposure_params`）⇒ 与 GUI 回填值口径一致。
  - PDF 与图片两条路径都支持；图片的「A4 快路径」在**该页有生效曝光**时必须让路。

★ 两层验证
  ① **路由层**：monkeypatch `_open_page_image`，记录它每页拿到的**有效曝光**
     ⇒ 直接证明「哪一页用了哪份曝光」，PDF 与图片路径都覆盖。
  ② **像素层**：真写文件，用 PIL 量亮度 ⇒ 证明覆盖**真的落到像素上**，
     且没覆盖的页与「不传覆盖」的基线**逐字节一致**。

★ 全部在临时目录里跑，**不连真机、不出纸**。
"""

import os

import pytest
from PIL import Image

import save_manager


# ── 夹具 ────────────────────────────────────────────────────────────────

@pytest.fixture
def gray_pages(tmp_path):
    """两张**灰色** JPEG（灰底 ⇒ 曝光效果最容易量出来）"""
    paths = []
    for i in range(2):
        p = tmp_path / ("page_%02d.jpg" % i)
        Image.new("RGB", (120, 160), (128, 128, 128)).save(p, "JPEG", quality=95)
        paths.append(str(p))
    return paths


def _mean_luma(path):
    """量**内容区中心**的平均亮度（先定位非白内容框，再取其中心）。

    ★ 为什么不能量整页：产物过了 A4 画布归一（`_a4_canvas`）⇒ 页面大部分是白边
      ⇒ 整页均值恒 ≈ 255 ⇒ 曝光效果被完全淹没（实测 254.85 vs 254.72）。
    ★ 为什么不能按比例裁中心：`_a4_canvas` 是把原图**贴到**画布上（**不放大**）
      ⇒ 小图在 2480×3507 的画布里只占中间一小块 ⇒ 固定比例的中心裁剪仍落在白边
      （实测 251.30 vs 248.00）。
    ★ 用 `tobytes()` 而不是 `getdata()`：后者在 Pillow 14 将被移除（DeprecationWarning）。
    """
    with Image.open(path) as im:
        g = im.convert("L")
        mask = g.point(lambda v: 255 if v < 245 else 0)
        bbox = mask.getbbox()
        if not bbox:
            return 255.0
        cx = (bbox[0] + bbox[2]) // 2
        cy = (bbox[1] + bbox[3]) // 2
        half = max(1, min(bbox[2] - bbox[0], bbox[3] - bbox[1]) // 6)
        data = g.crop((cx - half, cy - half, cx + half, cy + half)).tobytes()
        return sum(data) / float(len(data))


def _manual(brightness=0, contrast=0, gamma=1.0):
    return {
        "mode": "manual",
        "brightness": brightness,
        "contrast": contrast,
        "gamma": gamma,
        "shadows": 0,
        "highlights": 0,
        "channel_gains": [1.0, 1.0, 1.0],
    }


# ── ① 路由层：每页拿到的有效曝光 ─────────────────────────────────────────

class TestEffectiveExposureRouting:
    """monkeypatch `_open_page_image` ⇒ 直接看每页拿到的曝光是谁的"""

    @pytest.fixture
    def spy(self, monkeypatch):
        calls = []
        real = save_manager._open_page_image

        def _spy(cache_file, rotation=0, exposure=None, **kw):
            calls.append({"file": cache_file, "exposure": exposure})
            return real(cache_file, rotation, exposure, **kw)

        monkeypatch.setattr(save_manager, "_open_page_image", _spy)
        return calls

    def test_image_path_uses_page_override_then_document(self, tmp_path, gray_pages, spy):
        doc = _manual(brightness=10)
        override = _manual(brightness=60)
        save_manager.save_pages("j1", "jpg", str(tmp_path / "o.jpg"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)],
                                [[0], [1]], exposure=doc,
                                page_exposures=[override, None])
        assert len(spy) == 2, "应逐页打开两次，实际 %d" % len(spy)
        assert spy[0]["exposure"] == override, "第 1 页必须用自己的覆盖"
        assert spy[1]["exposure"] == doc, "第 2 页没有覆盖 ⇒ 必须跟随整批"

    def test_pdf_path_uses_page_override_then_document(self, tmp_path, gray_pages, spy):
        doc = _manual(brightness=10)
        override = _manual(brightness=60)
        save_manager.save_pages("j2", "pdf", str(tmp_path / "o.pdf"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)],
                                [[0], [1]], exposure=doc,
                                page_exposures=[override, None])
        assert len(spy) == 2
        assert spy[0]["exposure"] == override, "PDF 路径第 1 页必须用自己的覆盖"
        assert spy[1]["exposure"] == doc, "PDF 路径第 2 页必须跟随整批"

    def test_pdf_group_mapping_uses_global_page_index(self, tmp_path, gray_pages, spy):
        """★ 组内下标 ⇒ 必须先映射回**全局** page_specs 下标，
        否则「第 2 组的第 1 页」会错拿第 1 页的覆盖。"""
        doc = _manual(brightness=10)
        ov_a = _manual(brightness=60)
        ov_b = _manual(brightness=80)
        # 两组：第 1 组 = [0]，第 2 组 = [1]
        save_manager.save_pages("j3", "pdf", str(tmp_path / "o.pdf"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)],
                                [[0], [1]], exposure=doc,
                                page_exposures=[ov_a, ov_b])
        assert spy[0]["exposure"] == ov_a
        assert spy[1]["exposure"] == ov_b, "第 2 组必须拿到**它自己那页**的覆盖"

    def test_missing_override_falls_back_to_document(self, tmp_path, gray_pages, spy):
        doc = _manual(brightness=10)
        save_manager.save_pages("j4", "jpg", str(tmp_path / "o.jpg"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)],
                                [[0], [1]], exposure=doc,
                                page_exposures=[_manual(brightness=60)])  # 长度不足
        assert spy[0]["exposure"] is not doc
        assert spy[1]["exposure"] == doc, "列表长度不足 ⇒ 缺的页跟随整批"

    def test_non_dict_element_is_ignored(self, tmp_path, gray_pages, spy):
        """★ 元素不是 dict ⇒ 当作「没有覆盖」（跟随整批），不抛异常。"""
        doc = _manual(brightness=10)
        save_manager.save_pages("j5", "jpg", str(tmp_path / "o.jpg"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)],
                                [[0], [1]], exposure=doc,
                                page_exposures=["不是 dict", 123])
        assert spy[0]["exposure"] == doc
        assert spy[1]["exposure"] == doc

    def test_string_argument_is_ignored_by_type_gate(self, tmp_path, gray_pages, spy):
        """★ 类型闸：误传字符串 ⇒ 整体忽略（否则 "abc"[0] 会变成第 1 页的覆盖）。"""
        doc = _manual(brightness=10)
        save_manager.save_pages("j6", "jpg", str(tmp_path / "o.jpg"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)],
                                [[0], [1]], exposure=doc,
                                page_exposures="abc")
        assert spy[0]["exposure"] == doc
        assert spy[1]["exposure"] == doc

    def test_save_request_passes_page_exposures_through(self, tmp_path, gray_pages, spy):
        doc = _manual(brightness=10)
        ov = _manual(brightness=60)
        req = save_manager.SaveRequest(
            job_id="j7", ext="jpg", output_path=str(tmp_path / "o.jpg"),
            page_specs=[(gray_pages[0], 0), (gray_pages[1], 0)],
            groups=[[0], [1]], exposure=doc, page_exposures=[ov, None])
        save_manager.run_save_job(req)
        assert spy[0]["exposure"] == ov
        assert spy[1]["exposure"] == doc

    def test_default_is_none(self):
        req = save_manager.SaveRequest(job_id="j", ext="jpg", output_path="o.jpg",
                                       page_specs=[], groups=[])
        assert req.page_exposures is None


# ── ② 像素层：覆盖真的落到像素上 ─────────────────────────────────────────

class TestPerPageExposurePixels:
    """真写文件 ⇒ 用 PIL 量亮度"""

    def test_override_page_is_brighter_and_untouched_page_is_identical(
            self, tmp_path, gray_pages):
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        # 基线：整批不加曝光
        save_manager.save_pages("p1", "jpg", str(base_dir / "b.jpg"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)], [[0], [1]])
        # 覆盖：只给第 1 页加亮度
        save_manager.save_pages("p2", "jpg", str(out_dir / "b.jpg"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)], [[0], [1]],
                                page_exposures=[_manual(brightness=60), None])

        b0 = os.path.join(str(base_dir), "b_001_001.jpg")
        b1 = os.path.join(str(base_dir), "b_002_001.jpg")
        o0 = os.path.join(str(out_dir), "b_001_001.jpg")
        o1 = os.path.join(str(out_dir), "b_002_001.jpg")
        assert os.path.exists(b0) and os.path.exists(b1)
        assert os.path.exists(o0) and os.path.exists(o1)

        assert _mean_luma(o0) > _mean_luma(b0) + 20, (
            "有覆盖的那页必须明显变亮：%r vs %r" % (_mean_luma(o0), _mean_luma(b0)))
        assert open(o1, "rb").read() == open(b1, "rb").read(), (
            "没有覆盖的那页必须与基线**逐字节一致**")

    def test_without_page_exposures_bytes_identical_to_baseline(
            self, tmp_path, gray_pages):
        """★ 不传 `page_exposures` ⇒ 与改造前**逐字节一致**（向后兼容）。"""
        base_dir = tmp_path / "b"
        base_dir.mkdir()
        out_dir = tmp_path / "o"
        out_dir.mkdir()
        save_manager.save_pages("p3", "jpg", str(base_dir / "b.jpg"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)], [[0], [1]])
        save_manager.save_pages("p4", "jpg", str(out_dir / "b.jpg"),
                                [(gray_pages[0], 0), (gray_pages[1], 0)], [[0], [1]],
                                page_exposures=None)
        assert (open(os.path.join(str(base_dir), "b_001_001.jpg"), "rb").read()
                == open(os.path.join(str(out_dir), "b_001_001.jpg"), "rb").read())

    def test_mode_off_override_keeps_fast_path_bytes(self, tmp_path, gray_pages):
        """★ `mode="off"` 的覆盖 ⇒ 该页**不套曝光** ⇒ 走 A4 快路径 ⇒ 产物正常产出。"""
        out_dir = tmp_path / "o"
        out_dir.mkdir()
        save_manager.save_pages("p5", "jpg", str(out_dir / "b.jpg"),
                                [(gray_pages[0], 0)], [[0]],
                                page_exposures=[{"mode": "off"}])
        # 单组 ⇒ 图片产物名是 `{base}_{组内序号:03d}.{ext}`
        produced = os.path.join(str(out_dir), "b_001.jpg")
        assert os.path.exists(produced), "产物名应为 b_001.jpg，实际目录：%r" % os.listdir(str(out_dir))
        with Image.open(produced) as im:
            assert im.size[0] > 0 and im.size[1] > 0

    def test_pdf_pages_differ_when_one_page_overridden(self, tmp_path, gray_pages):
        """★ PDF 路径：只有第 1 页被覆盖 ⇒ 产出的 PDF 与全批一致时**不同**。"""
        plain = tmp_path / "plain.pdf"
        over = tmp_path / "over.pdf"
        save_manager.save_pages("p6", "pdf", str(plain),
                                [(gray_pages[0], 0), (gray_pages[1], 0)], [[0], [1]])
        save_manager.save_pages("p7", "pdf", str(over),
                                [(gray_pages[0], 0), (gray_pages[1], 0)], [[0], [1]],
                                page_exposures=[_manual(brightness=60), None])
        # 两组 ⇒ PDF 产物名是 `{base}_{组号:03d}.pdf`
        plain_1 = str(tmp_path / "plain_001.pdf")
        over_1 = str(tmp_path / "over_001.pdf")
        assert os.path.exists(plain_1) and os.path.exists(over_1), (
            "产物名应为 plain_001.pdf / over_001.pdf，实际目录：%r" % os.listdir(str(tmp_path)))
        assert open(plain_1, "rb").read() != open(over_1, "rb").read(), (
            "逐页覆盖必须真的改变 PDF 产物")
