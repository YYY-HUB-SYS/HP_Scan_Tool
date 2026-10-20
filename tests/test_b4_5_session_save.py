"""
批次 4 · 会话级落盘（B4-5）单元测试 — HP Scan Tool v4.0

覆盖：
  A 页序   ：双面交错 / 批次拼接（**用 doc_merge 自己算一遍做对照**）
  B 产物   ：PDF 一个文件多页 / 图片每页一个文件且全局页号连续 / 多组命名
  C 纠偏   ：会话页施加纠偏角 / force_keep 的页不纠偏
  D 清理   ：落盘成功后清 job 目录 + 解除保护 / 失败后什么都不清（可重试）
  E 门禁   ：体积预检走 SaveAborted（不静默截断）/ 空会话明确报错

★ 真实样本：`.verify_out/D1_platen/HP_Scan_20261020_210142_001.jpg`（真机件，
  2480×3507 / 300dpi / RGB）。斜页用**程序旋转**该真机件生成（不跑真机扫描）。
★ 不跑真机扫描、不读写 %APPDATA%\\HP_Scan_Tool（conftest 已把 APPDATA 重定向）。
"""

import io
import os
import sys

import pytest
from PIL import Image

import cache_manager
import doc_merge
import page_meta
import save_manager
from document_session import new_session

# 共享的"真实斜页"夹具（见 skew_fixtures 模块 docstring：为什么不能直接
# `rotate(fillcolor=255)` —— Pillow 对 RGB 的标量 fillcolor 是打包整数 ⇒ 纯红）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from skew_fixtures import (  # noqa: E402
    add_text_rows, corner_pixels, rotate_white,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_SAMPLE = os.path.join(PROJECT_ROOT, ".verify_out", "D1_platen",
                           "HP_Scan_20261020_210142_001.jpg")

# 6 种区分度高的颜色（RGB）：用于从产物里反查页序
RED = (220, 30, 30)
GREEN = (30, 200, 30)
BLUE = (30, 30, 220)
YELLOW = (240, 230, 30)
MAGENTA = (210, 30, 210)
CYAN = (30, 200, 200)


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """隔离的缓存根目录（缓存件写在真 job 目录里 ⇒ 落盘后应被清掉）"""
    root = str(tmp_path / "cache")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: root)
    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: os.path.join(root, "cache_config.json"))
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(cache_manager, "_active_jobs", set())
    monkeypatch.setattr(cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(cache_manager, "_cache_size_cache", None)
    return root


@pytest.fixture(scope="module")
def real_page():
    """真机扫描件（PIL 图像，模块级只读一次）"""
    if not os.path.exists(REAL_SAMPLE):
        pytest.skip(f"真机样本不存在: {REAL_SAMPLE}")
    with Image.open(REAL_SAMPLE) as im:
        im.load()
        return im.copy()


@pytest.fixture(scope="module")
def small_page(real_page):
    """真机件的半尺寸版（检测/纠偏结论一致，快 4 倍）"""
    return real_page.resize((real_page.width // 2, real_page.height // 2),
                            Image.LANCZOS)


# ── 工具 ──────────────────────────────────────────────────────────────


def _jpeg_bytes(img: Image.Image, quality: int = 95) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _solid_bytes(color, size=(600, 800)) -> bytes:
    """纯色页字节"""
    return _jpeg_bytes(Image.new("RGB", size, color))


def _skewed_bytes(page: Image.Image, angle: float) -> bytes:
    """把真机件程序旋转 angle 度 ⇒ 斜页字节（angle>0 = 逆时针）

    ★ 真机件近乎空白（ink ≈ 0.00022 < 检测下限 0.005）⇒ 需合成文字行结构
    ★ 旋转填充必须**白**：Pillow 对 RGB 的标量 fillcolor=255 是**打包整数**
      ⇒ (255,0,0) 纯红；红框会替代内容成为"检测力来源"，并在纠偏后残留红角、
      掩盖残余误差。
    """
    return _jpeg_bytes(rotate_white(add_text_rows(page), angle))


def _write_job_pages(cache_root, job_id, pages, deskew_angle=0.0,
                     manual_rotation=0):
    """
    往缓存里写一个 job 的若干页（**写真 job 目录** ⇒ 落盘后应被清掉）。

    pages: [(page_no, color), ...]
    返回 {page_no: 缓存件路径}
    """
    paths = {}
    for page_no, color in pages:
        data = _solid_bytes(color)
        path = cache_manager.write_page(job_id, page_no, data, "jpg")
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
        page_meta.PageMetaStore(cache_root).set_page(
            job_id, page_no,
            page_meta.PageMeta(cache_path=path, width=width, height=height,
                               manual_rotation=manual_rotation,
                               deskew_angle=deskew_angle))
        paths[page_no] = path
    return paths


def _pdf_page_colors(path):
    """读出 PDF 每一页的**中心颜色**（用来反查页序）"""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    colors = []
    for page in reader.pages:
        images = page.images
        assert images, "PDF 页里没有内嵌图片"
        with Image.open(io.BytesIO(images[0].data)) as im:
            im = im.convert("RGB")
            width, height = im.size
            colors.append(im.getpixel((width // 2, height // 2)))
    return colors


def _image_center_color(path):
    with Image.open(str(path)) as im:
        im = im.convert("RGB")
        width, height = im.size
        return im.getpixel((width // 2, height // 2))


def _assert_color(actual, expected, tol=12):
    assert len(actual) == len(expected), \
        f"页数不符: 实际 {len(actual)}，期望 {len(expected)}"
    for act, exp in zip(actual, expected):
        assert all(abs(a - b) <= tol for a, b in zip(act, exp)), \
            f"颜色不符: 实际 {actual}，期望 {expected}"


def _expected_duplex_order(session):
    """期望页序：交给 doc_merge 自己算（**不手写期望值**）"""
    front = [p for p in session.pages if p.side == "front"]
    back = [p for p in session.pages if p.side == "back"]
    return doc_merge.interleave_duplex(front, back,
                                       reverse_back=session.reverse_back)


def _expected_batch_order(session):
    """期望页序：按 job 分组（首次出现顺序）后交给 doc_merge"""
    batches = []
    index = {}
    for page in session.pages:
        if page.job_id not in index:
            index[page.job_id] = len(batches)
            batches.append([])
        batches[index[page.job_id]].append(page)
    return doc_merge.merge_batches(batches)


def _colors_of(order, color_map):
    return [color_map[(p.job_id, p.page_no)] for p in order]


# ── A：页序 ──────────────────────────────────────────────────────────


class TestDuplexOrder:
    def test_three_plus_three_one_pdf_six_pages(self, tmp_path, cache_root):
        """★ 双面 3 正 + 3 反 ⇒ 一个 PDF、6 页、页序 = interleave_duplex 的输出"""
        color_map = {}
        front_colors = [RED, GREEN, BLUE]
        back_colors = [YELLOW, MAGENTA, CYAN]

        front_paths = _write_job_pages(cache_root, "job_dup_front",
                                       [(i + 1, c) for i, c in
                                        enumerate(front_colors)])
        back_paths = _write_job_pages(cache_root, "job_dup_back",
                                      [(i + 1, c) for i, c in
                                       enumerate(back_colors)])
        for i, c in enumerate(front_colors):
            color_map[("job_dup_front", i + 1)] = c
        for i, c in enumerate(back_colors):
            color_map[("job_dup_back", i + 1)] = c

        session = new_session(mode="duplex", ext="pdf", dpi=300)
        session.add_pages("job_dup_front", list(front_paths.values()),
                          side="front", page_nos=[1, 2, 3])
        session.add_pages("job_dup_back", list(back_paths.values()),
                          side="back", page_nos=[1, 2, 3])

        out = str(tmp_path / "duplex.pdf")
        result = save_manager.save_session(session, out)

        assert len(result) == 1, "双面会话必须只产出一个 PDF"
        assert result[0] == out and os.path.exists(out)
        assert result.saved_page_count == 6
        colors = _pdf_page_colors(out)
        assert len(colors) == 6, f"PDF 应有 6 页，实际 {len(colors)}"
        _assert_color(colors, _colors_of(_expected_duplex_order(session),
                                         color_map))

    def test_three_plus_two_last_page_is_front(self, tmp_path, cache_root):
        """双面 3 正 + 2 反（末页只有一面）⇒ 5 页、末位是正面"""
        color_map = {}
        front_colors = [RED, GREEN, BLUE]
        back_colors = [YELLOW, MAGENTA]

        front_paths = _write_job_pages(cache_root, "job_dup2_front",
                                       [(i + 1, c) for i, c in
                                        enumerate(front_colors)])
        back_paths = _write_job_pages(cache_root, "job_dup2_back",
                                      [(i + 1, c) for i, c in
                                       enumerate(back_colors)])
        for i, c in enumerate(front_colors):
            color_map[("job_dup2_front", i + 1)] = c
        for i, c in enumerate(back_colors):
            color_map[("job_dup2_back", i + 1)] = c

        session = new_session(mode="duplex", ext="pdf", dpi=300)
        session.add_pages("job_dup2_front", list(front_paths.values()),
                          side="front", page_nos=[1, 2, 3])
        session.add_pages("job_dup2_back", list(back_paths.values()),
                          side="back", page_nos=[1, 2])

        out = str(tmp_path / "duplex_odd.pdf")
        result = save_manager.save_session(session, out)

        assert result.saved_page_count == 5
        colors = _pdf_page_colors(out)
        assert len(colors) == 5
        expected = _colors_of(_expected_duplex_order(session), color_map)
        _assert_color(colors, expected)
        assert expected[-1] == BLUE, "末位应是多出来的那张正面"

    def test_reverse_back_flag_is_respected(self, tmp_path, cache_root):
        """reverse_back=False ⇒ 背面不反转（页序跟着变）"""
        color_map = {}
        front_paths = _write_job_pages(cache_root, "job_rev_front",
                                       [(1, RED), (2, GREEN)])
        back_paths = _write_job_pages(cache_root, "job_rev_back",
                                      [(1, YELLOW), (2, MAGENTA)])
        color_map[("job_rev_front", 1)] = RED
        color_map[("job_rev_front", 2)] = GREEN
        color_map[("job_rev_back", 1)] = YELLOW
        color_map[("job_rev_back", 2)] = MAGENTA

        session = new_session(mode="duplex", ext="pdf", dpi=300,
                              reverse_back=False)
        session.add_pages("job_rev_front", list(front_paths.values()),
                          side="front", page_nos=[1, 2])
        session.add_pages("job_rev_back", list(back_paths.values()),
                          side="back", page_nos=[1, 2])

        out = str(tmp_path / "norev.pdf")
        save_manager.save_session(session, out)
        _assert_color(_pdf_page_colors(out),
                      _colors_of(_expected_duplex_order(session), color_map))
        # 反转时背面顺序相反 ⇒ 两种模式的页序必须不同
        assert _expected_duplex_order(session)[1].job_id == "job_rev_back"
        assert _expected_duplex_order(session)[1].page_no == 1


class TestBatchOrder:
    def test_two_batches_five_pages_one_pdf(self, tmp_path, cache_root):
        """★ 批次会话：2 批共 5 页 ⇒ 一个 PDF、5 页、顺序 = 批次顺序 + 批内原序"""
        color_map = {}
        first = [RED, GREEN, BLUE]
        second = [YELLOW, MAGENTA]

        paths_a = _write_job_pages(cache_root, "job_batch_a",
                                   [(i + 1, c) for i, c in enumerate(first)])
        paths_b = _write_job_pages(cache_root, "job_batch_b",
                                   [(i + 1, c) for i, c in enumerate(second)])
        for i, c in enumerate(first):
            color_map[("job_batch_a", i + 1)] = c
        for i, c in enumerate(second):
            color_map[("job_batch_b", i + 1)] = c

        session = new_session(mode="batch", ext="pdf", dpi=300)
        session.add_pages("job_batch_a", list(paths_a.values()), side="single",
                          page_nos=[1, 2, 3])
        session.add_pages("job_batch_b", list(paths_b.values()), side="single",
                          page_nos=[1, 2])

        out = str(tmp_path / "batch.pdf")
        result = save_manager.save_session(session, out)

        assert len(result) == 1
        assert result.saved_page_count == 5
        colors = _pdf_page_colors(out)
        assert len(colors) == 5
        _assert_color(colors, _colors_of(_expected_batch_order(session),
                                         color_map))


# ── B：产物 ──────────────────────────────────────────────────────────


class TestOutputArtifacts:
    def test_image_session_global_page_numbers_continuous(self, tmp_path,
                                                          cache_root):
        """★ 图片格式：每页一个文件，**全局页号连续**（跨批次不重号）"""
        color_map = {}
        first = [RED, GREEN, BLUE]
        second = [YELLOW, MAGENTA]

        paths_a = _write_job_pages(cache_root, "job_img_a",
                                   [(i + 1, c) for i, c in enumerate(first)])
        paths_b = _write_job_pages(cache_root, "job_img_b",
                                   [(i + 1, c) for i, c in enumerate(second)])
        for i, c in enumerate(first):
            color_map[("job_img_a", i + 1)] = c
        for i, c in enumerate(second):
            color_map[("job_img_b", i + 1)] = c

        session = new_session(mode="batch", ext="jpg", dpi=300)
        session.add_pages("job_img_a", list(paths_a.values()), side="single",
                          page_nos=[1, 2, 3])
        session.add_pages("job_img_b", list(paths_b.values()), side="single",
                          page_nos=[1, 2])

        out_base = str(tmp_path / "imgs")
        result = save_manager.save_session(session, out_base)

        assert len(result) == 5
        names = sorted(os.path.basename(p) for p in result)
        assert names == ["imgs_001.jpg", "imgs_002.jpg", "imgs_003.jpg",
                         "imgs_004.jpg", "imgs_005.jpg"], names
        colors = [_image_center_color(p) for p in result]
        _assert_color(colors, _colors_of(_expected_batch_order(session),
                                         color_map))

    def test_multi_group_naming_follows_build_output_name(self, tmp_path,
                                                           cache_root):
        """★ 多组场景 ⇒ 命名符合 build_output_name 的多组规则"""
        paths = _write_job_pages(cache_root, "job_group",
                                 [(i + 1, c) for i, c in
                                  enumerate([RED, GREEN, BLUE, YELLOW])])
        session = new_session(mode="single", ext="pdf", dpi=300)
        session.add_pages("job_group", list(paths.values()), side="single",
                          page_nos=[1, 2, 3, 4])

        out_base = str(tmp_path / "grp")
        result = save_manager.save_session(session, out_base,
                                           groups=[[0, 1], [2, 3]])

        expected = [save_manager.build_output_name(out_base, "pdf", 2, 1, 1),
                    save_manager.build_output_name(out_base, "pdf", 2, 2, 1)]
        assert list(result) == expected
        assert all(os.path.exists(p) for p in expected)

    def test_multi_group_image_naming(self, tmp_path, cache_root):
        """多组 + 图片 ⇒ {base}_{组号}_{组内序号}.ext"""
        paths = _write_job_pages(cache_root, "job_group_img",
                                 [(i + 1, c) for i, c in
                                  enumerate([RED, GREEN, BLUE])])
        session = new_session(mode="single", ext="jpg", dpi=300)
        session.add_pages("job_group_img", list(paths.values()), side="single",
                          page_nos=[1, 2, 3])

        out_base = str(tmp_path / "grpimg")
        result = save_manager.save_session(session, out_base,
                                           groups=[[0], [1, 2]])

        names = sorted(os.path.basename(p) for p in result)
        assert names == ["grpimg_001_001.jpg", "grpimg_002_001.jpg",
                         "grpimg_002_002.jpg"], names

    def test_extra_dirs_get_a_copy(self, tmp_path, cache_root):
        """额外输出目录：产物复制一份过去"""
        paths = _write_job_pages(cache_root, "job_extra",
                                 [(1, RED), (2, GREEN)])
        session = new_session(mode="single", ext="pdf", dpi=300)
        session.add_pages("job_extra", list(paths.values()), side="single",
                          page_nos=[1, 2])

        extra = tmp_path / "extra"
        out = str(tmp_path / "extra_out.pdf")
        result = save_manager.save_session(session, out, extra_dirs=[str(extra)])

        assert os.path.exists(os.path.join(str(extra),
                                           os.path.basename(result[0])))


# ── C：纠偏 ──────────────────────────────────────────────────────────


class TestDeskewOnSessionSave:
    def test_deskew_angle_is_applied(self, tmp_path, cache_root, small_page):
        """★ 会话页的 deskew_angle 被施加到产物（比不纠偏更正）"""
        import skew_detect

        job_skew = "job_skew_apply"
        job_plain = "job_skew_plain"
        data = _skewed_bytes(small_page, 3.0)

        skew_path = cache_manager.write_page(job_skew, 1, data, "jpg")
        plain_path = cache_manager.write_page(job_plain, 1, data, "jpg")
        store = page_meta.PageMetaStore(cache_root)
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
        store.set_page(job_skew, 1, page_meta.PageMeta(
            cache_path=skew_path, width=width, height=height,
            deskew_angle=-3.0))
        store.set_page(job_plain, 1, page_meta.PageMeta(
            cache_path=plain_path, width=width, height=height,
            deskew_angle=0.0))

        session = new_session(mode="single", ext="jpg", dpi=300)
        session.add_pages(job_skew, [skew_path], side="single", page_nos=[1])
        out_skew = str(tmp_path / "skew")
        save_manager.save_session(session, out_skew)

        session2 = new_session(mode="single", ext="jpg", dpi=300)
        session2.add_pages(job_plain, [plain_path], side="single", page_nos=[1])
        out_plain = str(tmp_path / "plain")
        save_manager.save_session(session2, out_plain)

        skew_out = save_manager.build_output_name(out_skew, "jpg", 1, 1, 1)
        plain_out = save_manager.build_output_name(out_plain, "jpg", 1, 1, 1)
        with Image.open(skew_out) as im:
            skew_angle = skew_detect.estimate_skew(im)
        with Image.open(plain_out) as im:
            plain_angle = skew_detect.estimate_skew(im)

        assert plain_angle is not None and abs(plain_angle + 3.0) <= 0.6, \
            f"对照产物应仍是斜的，实际 {plain_angle}"
        assert skew_angle is None or abs(skew_angle) <= 0.5, \
            f"纠偏后应接近 0 度，实际 {skew_angle}"

    def test_force_keep_page_is_not_deskewed(self, tmp_path, cache_root,
                                             small_page):
        """★ force_keep=True 的页**不纠偏**（产物与缓存原件逐字节相同）"""
        job = "job_force_keep"
        data = _skewed_bytes(small_page, 3.0)
        path = cache_manager.write_page(job, 1, data, "jpg")
        store = page_meta.PageMetaStore(cache_root)
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
        store.set_page(job, 1, page_meta.PageMeta(
            cache_path=path, width=width, height=height, deskew_angle=-3.0))

        session = new_session(mode="single", ext="jpg", dpi=300)
        refs = session.add_pages(job, [path], side="single", page_nos=[1])
        refs[0].force_keep = True

        out_base = str(tmp_path / "force")
        result = save_manager.save_session(session, out_base)

        out = save_manager.build_output_name(out_base, "jpg", 1, 1, 1)
        assert list(result) == [out]
        assert open(out, "rb").read() == data, \
            "force_keep 的页必须原样保存（不纠偏）"

    def test_non_force_keep_page_is_deskewed(self, tmp_path, cache_root,
                                              small_page):
        """对照组：同样的页不设 force_keep ⇒ 会被纠偏（产物与原件不同）"""
        job = "job_no_force"
        data = _skewed_bytes(small_page, 3.0)
        path = cache_manager.write_page(job, 1, data, "jpg")
        store = page_meta.PageMetaStore(cache_root)
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
        store.set_page(job, 1, page_meta.PageMeta(
            cache_path=path, width=width, height=height, deskew_angle=-3.0))

        session = new_session(mode="single", ext="jpg", dpi=300)
        session.add_pages(job, [path], side="single", page_nos=[1])
        out_base = str(tmp_path / "noforce")
        save_manager.save_session(session, out_base)

        out = save_manager.build_output_name(out_base, "jpg", 1, 1, 1)
        assert open(out, "rb").read() != data, \
            "没设 force_keep 的页必须施加纠偏"


# ── D：清理 ──────────────────────────────────────────────────────────


class TestCleanup:
    def test_success_clears_job_dirs_and_releases_protection(self, tmp_path,
                                                               cache_root):
        """★ 落盘成功后：job 目录被清掉、保护被解除"""
        job = "job_cleanup_ok"
        paths = _write_job_pages(cache_root, job, [(1, RED), (2, GREEN)])
        cache_manager.protect_dir(job)
        assert cache_manager.is_job_protected(job)

        session = new_session(mode="single", ext="pdf", dpi=300)
        session.add_pages(job, list(paths.values()), side="single",
                          page_nos=[1, 2])

        out = str(tmp_path / "cleanup.pdf")
        result = save_manager.save_session(session, out)

        assert len(result) == 1 and os.path.exists(out)
        assert not os.path.isdir(os.path.join(cache_root, job)), \
            "落盘成功后 job 目录必须被清掉"
        assert not cache_manager.is_job_protected(job), \
            "落盘成功后保护必须被解除"
        # 页元数据也一并清掉
        assert page_meta.PageMetaStore(cache_root).get_job(job) == {}

    def test_all_referenced_jobs_are_cleared(self, tmp_path, cache_root):
        """多 job 会话 ⇒ 全部 job 目录都被清掉"""
        job_a = "job_multi_a"
        job_b = "job_multi_b"
        paths_a = _write_job_pages(cache_root, job_a, [(1, RED)])
        paths_b = _write_job_pages(cache_root, job_b, [(1, GREEN)])
        cache_manager.protect_dir(job_a)
        cache_manager.protect_dir(job_b)

        session = new_session(mode="batch", ext="pdf", dpi=300)
        session.add_pages(job_a, list(paths_a.values()), side="single",
                          page_nos=[1])
        session.add_pages(job_b, list(paths_b.values()), side="single",
                          page_nos=[1])

        out = str(tmp_path / "multi.pdf")
        save_manager.save_session(session, out)

        for job in (job_a, job_b):
            assert not os.path.isdir(os.path.join(cache_root, job))
            assert not cache_manager.is_job_protected(job)

    def test_failure_keeps_job_dir_and_allows_retry(self, tmp_path, cache_root,
                                                    monkeypatch):
        """★ 落盘失败后：job 目录仍在（可重试）"""
        job = "job_cleanup_fail"
        paths = _write_job_pages(cache_root, job, [(1, RED), (2, GREEN)])
        cache_manager.protect_dir(job)

        session = new_session(mode="single", ext="pdf", dpi=300)
        session.add_pages(job, list(paths.values()), side="single",
                          page_nos=[1, 2])

        # 体积预检超限 ⇒ SaveAborted（什么都不清）
        monkeypatch.setattr(save_manager, "PDF_PEAK_LIMIT_BYTES", 1)
        out = str(tmp_path / "retry.pdf")
        with pytest.raises(save_manager.SaveAborted):
            save_manager.save_session(session, out, confirm=lambda msg: False)

        assert os.path.isdir(os.path.join(cache_root, job)), \
            "落盘失败后 job 目录必须保留（可重试）"
        assert cache_manager.is_job_protected(job)

        # 重试（恢复正常门禁）⇒ 成功并清理
        monkeypatch.setattr(save_manager, "PDF_PEAK_LIMIT_BYTES",
                            save_manager.PDF_PEAK_LIMIT_BYTES)
        result = save_manager.save_session(session, out)
        assert len(result) == 1 and os.path.exists(out)
        assert not os.path.isdir(os.path.join(cache_root, job))

    def test_partial_failure_keeps_cache(self, tmp_path, cache_root):
        """部分页失败 ⇒ 缓存保留（页数与产物对不上时不能删原件）"""
        job = "job_partial"
        paths = _write_job_pages(cache_root, job, [(1, RED), (2, GREEN)])
        # 把第 2 页的缓存件换成坏字节 ⇒ 该页转换失败
        with open(paths[2], "wb") as f:
            f.write(b"not-an-image")

        session = new_session(mode="single", ext="pdf", dpi=300)
        session.add_pages(job, list(paths.values()), side="single",
                          page_nos=[1, 2])

        out = str(tmp_path / "partial.pdf")
        result = save_manager.save_session(session, out)

        assert result.failed_pages == 1
        assert result.saved_page_count == 1
        assert os.path.isdir(os.path.join(cache_root, job)), \
            "有页失败时缓存必须保留（可重试）"


# ── E：门禁 ──────────────────────────────────────────────────────────


class TestGuards:
    def test_volume_precheck_raises_save_aborted_without_output(self, tmp_path,
                                                                 cache_root,
                                                                 monkeypatch):
        """★ 体积预检超限 ⇒ 走既有 SaveAborted 路径，**不静默截断**"""
        job = "job_volume"
        paths = _write_job_pages(cache_root, job, [(1, RED), (2, GREEN)])
        session = new_session(mode="single", ext="pdf", dpi=300)
        session.add_pages(job, list(paths.values()), side="single",
                          page_nos=[1, 2])

        monkeypatch.setattr(save_manager, "PDF_PEAK_LIMIT_BYTES", 1)
        out = str(tmp_path / "volume.pdf")
        seen = []

        def confirm(msg):
            seen.append(msg)
            return False

        with pytest.raises(save_manager.SaveAborted):
            save_manager.save_session(session, out, confirm=confirm)

        assert seen, "体积预检必须提示用户"
        assert not os.path.exists(out), "预检失败不得产出任何文件"
        assert os.path.isdir(os.path.join(cache_root, job)), \
            "预检失败不得清理缓存"

    def test_volume_precheck_without_confirm_only_warns(self, tmp_path,
                                                         cache_root,
                                                         monkeypatch):
        """无 confirm 回调 ⇒ 只记 warning 并继续（既有语义）"""
        job = "job_volume_warn"
        paths = _write_job_pages(cache_root, job, [(1, RED)])
        session = new_session(mode="single", ext="pdf", dpi=300)
        session.add_pages(job, list(paths.values()), side="single",
                          page_nos=[1])

        monkeypatch.setattr(save_manager, "PDF_PEAK_LIMIT_BYTES", 1)
        out = str(tmp_path / "volume_warn.pdf")
        result = save_manager.save_session(session, out)

        assert result.warnings, "预检提示必须进 warnings"
        assert len(result) == 1 and os.path.exists(out)

    def test_empty_session_raises_and_writes_no_file(self, tmp_path):
        """★ 空会话（0 页）⇒ 明确报错，不产出 0 字节文件"""
        session = new_session(mode="batch", ext="pdf", dpi=300)
        out = str(tmp_path / "empty.pdf")

        with pytest.raises(RuntimeError) as excinfo:
            save_manager.save_session(session, out)

        assert "没有可保存的页面" in str(excinfo.value)
        assert not os.path.exists(out), "空会话不得产出任何文件"

    def test_missing_output_path_raises(self, tmp_path, cache_root):
        """既没 out_path 也没 session.output_path ⇒ 明确报错"""
        job = "job_no_path"
        paths = _write_job_pages(cache_root, job, [(1, RED)])
        session = new_session(mode="single", ext="pdf", dpi=300)
        session.add_pages(job, list(paths.values()), side="single",
                          page_nos=[1])

        with pytest.raises(ValueError):
            save_manager.save_session(session)

    def test_result_is_save_result(self, tmp_path, cache_root):
        """返回 SaveResult（list 子类），字段与既有 save_pages 一致"""
        job = "job_result"
        paths = _write_job_pages(cache_root, job, [(1, RED)])
        session = new_session(mode="single", ext="pdf", dpi=300)
        session.add_pages(job, list(paths.values()), side="single",
                          page_nos=[1])

        out = str(tmp_path / "result.pdf")
        result = save_manager.save_session(session, out)
        assert isinstance(result, save_manager.SaveResult)
        assert isinstance(result, list)
        assert result.failed_pages == 0
        assert result.saved_page_count == 1
        assert result.ok
