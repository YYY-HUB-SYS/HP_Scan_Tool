"""
轮 1 · 片 3 修复回归测试（GUI 层）—— HP Scan Tool v4.0

覆盖 review-round1-slice3 的每一条 finding：

  I-1  直接保存多页只报第 1 个路径 / 部分页失败只写日志 ⇒ 用户无感知
  I-2  直接保存与预览保存的文件名不一致（`_001` 后缀）
  I-3  单页裁边把 JPEG 字节覆盖进 .png 缓存原件（扩展名撒谎 + 覆盖原件）
  I-4  裁边后 page_meta 仍记原图宽高、crop_rect 从不写
  I-5  文档承诺的「页面分组」没有 UI 入口（splits 恒为空 ⇒ 死代码）
  I-6  直接保存静默套用上一次的曝光参数
  M-1  曝光手动模式主线程耗时注释不准（0.022s ⇒ 实测 0.130s）
  F-8  三处 register_job 没有对应注销 ⇒ job 永久受保护，缓存目录永不回收
  F-5  多页保存阻塞 GUI 主线程

GUI 一律 offscreen 跑（QT_QPA_PLATFORM=offscreen），不弹真实窗口，
不注入任何鼠标/键盘事件；缓存/配置/历史全部重定向到临时目录。
"""

import io
import os
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest
from PIL import Image

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
# 测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_gui_slice3_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import cache_manager
    import page_meta
    import save_manager
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)


@pytest.fixture(scope="module")
def qapp():
    """整个模块共用一个 QApplication（offscreen，不显示窗口）"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeMessageBox:
    """QMessageBox 替身：记录提示调用，绝不弹窗"""

    Yes = 0x00004000
    No = 0x00010000

    def __init__(self):
        self.critical_calls = []
        self.information_calls = []
        self.warning_calls = []
        self.question_reply = self.Yes

    def critical(self, parent, title, text):
        self.critical_calls.append((title, text))

    def information(self, parent, title, text):
        self.information_calls.append((title, text))

    def warning(self, parent, title, text):
        self.warning_calls.append((title, text))

    def question(self, parent, title, text):
        return self.question_reply


class _StubApp:
    """ScanApp 的最小替身：只借用直接保存相关的方法。"""

    _on_scan_finished = gui.ScanApp._on_scan_finished
    _direct_save_result = gui.ScanApp._direct_save_result
    _reset_exposure_state = gui.ScanApp._reset_exposure_state

    def __init__(self, direct_save=True, selected=None, extra_dirs=None,
                 exposure=None):
        self.direct_save_val = direct_save
        self.selected = selected
        self.extra_dirs = [] if extra_dirs is None else extra_dirs
        self.messages = []
        self.preview_calls = []
        self.status_bar = types.SimpleNamespace(showMessage=self.messages.append)
        self._exposure_state = exposure

    def _reset_scan_ui(self):
        pass

    def _show_preview(self, *args, **kwargs):
        self.preview_calls.append(("single", args, kwargs))

    def _show_multi_preview(self, *args, **kwargs):
        self.preview_calls.append(("multi", args, kwargs))


@pytest.fixture
def no_popups(monkeypatch):
    """所有可能弹窗的地方都换成替身（绝不弹真实窗口）"""
    fake = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake)
    monkeypatch.setattr(gui.QMessageBox, "critical", staticmethod(fake.critical))
    monkeypatch.setattr(gui.QMessageBox, "information", staticmethod(fake.information))
    monkeypatch.setattr(gui.QMessageBox, "warning", staticmethod(fake.warning))
    monkeypatch.setattr(gui.QMessageBox, "question", staticmethod(lambda *a, **k: fake.Yes))
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    return fake


def _wait_for(predicate, timeout=15.0, qapp=None):
    """泵事件循环直到 predicate 为真（超时返回 False）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        if qapp is not None:
            qapp.processEvents()
        time.sleep(0.01)
    return predicate()


def _make_result(tmp_path, page_count=1, ext="jpg", job_id="job_slice3"):
    return types.SimpleNamespace(
        job_id=job_id,
        ext=ext,
        page_count=page_count,
        cached_paths=[str(Path(tmp_path) / f"cache_{i:03d}.{ext}")
                      for i in range(1, page_count + 1)],
        output_path=str(Path(tmp_path) / f"scan.{ext}"),
    )


def _write_page(job_id, page_num, img, ext="jpg"):
    """往隔离缓存里写一页，返回缓存文件路径"""
    buf = io.BytesIO()
    if ext == "png":
        img.save(buf, "PNG")
    else:
        img.save(buf, "JPEG")
    return cache_manager.write_page(job_id, page_num, buf.getvalue(), ext)


def _scan_with_border(w=100, h=120, border=30):
    """造一张带大片白边的图（auto_crop 能裁出内容）"""
    img = Image.new("RGB", (w, h), "white")
    img.paste(Image.new("RGB", (w - 2 * border, h - 2 * border), "black"),
              (border, border))
    return img


# ────────── I-1 直接保存的提示要报"实际产出文件数" ──────────

def test_direct_save_reports_total_file_count(monkeypatch, tmp_path):
    """多页直接保存 ⇒ 提示里必须出现"共 N 个文件"，不能只报第 1 个路径"""
    fake = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake)

    files = [str(Path(tmp_path) / f"scan_{i:03d}.jpg") for i in range(1, 4)]
    monkeypatch.setattr(
        gui.save_manager, "save_pages",
        lambda *a, **k: gui.save_manager.SaveResult(files, failed_pages=0))

    app = _StubApp(direct_save=True)
    app._on_scan_finished("job_slice3", _make_result(tmp_path, page_count=3))

    assert fake.information_calls, "保存完必须有提示"
    text = fake.information_calls[0][1]
    assert "共 3 个文件" in text, f"提示没报文件总数: {text!r}"
    for f in files:
        assert os.path.basename(f) in text, f"提示里没有 {f}"


def test_direct_save_reports_failed_page_count(monkeypatch, tmp_path):
    """部分页保存失败 ⇒ 提示里必须出现失败数（save_manager.failed_pages）"""
    fake = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake)

    files = [str(Path(tmp_path) / f"scan_{i:03d}.jpg") for i in range(1, 3)]
    monkeypatch.setattr(
        gui.save_manager, "save_pages",
        lambda *a, **k: gui.save_manager.SaveResult(files, failed_pages=1))

    app = _StubApp(direct_save=True)
    app._on_scan_finished("job_slice3", _make_result(tmp_path, page_count=3))

    assert fake.information_calls
    text = fake.information_calls[0][1]
    assert "1/3 页" in text, f"提示里没有失败页数: {text!r}"
    assert "保存失败" in text


def test_direct_save_failure_still_critical(monkeypatch, tmp_path):
    """全部页都失败 ⇒ save_manager 抛异常 ⇒ 必须弹错误（不是静默）"""
    fake = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake)

    def _boom(*a, **k):
        raise RuntimeError("磁盘写入失败")

    monkeypatch.setattr(gui.save_manager, "save_pages", _boom)

    app = _StubApp(direct_save=True)
    app._on_scan_finished("job_slice3", _make_result(tmp_path, page_count=3))

    assert fake.critical_calls, "保存失败必须弹错误提示"
    assert not fake.information_calls


# ────────── I-2 两条保存路径的文件名必须一致 ──────────

def test_direct_and_preview_save_same_filename(qapp, tmp_path, isolated_cache,
                                               tmp_config_dir, monkeypatch,
                                               no_popups):
    """同一份扫描：直接保存与预览保存产出的**文件名完全相同**"""
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)

    cache = tmp_path / "page_001.jpg"
    Image.new("RGB", (60, 80), (200, 220, 240)).save(cache, "JPEG")
    output_path = tmp_path / "scan.jpg"

    # ① 直接保存
    result = types.SimpleNamespace(
        job_id="job_i2", ext="jpg", page_count=1,
        cached_paths=[str(cache)], output_path=str(output_path))
    app = _StubApp(direct_save=True)
    app._on_scan_finished("job_i2", result)
    direct_files = sorted(p.name for p in tmp_path.glob("scan*.jpg"))

    # ② 预览保存
    dlg = gui.PreviewDialog(None, str(cache), "jpg", str(output_path), "job_i2b")
    try:
        dlg._confirm()
    finally:
        dlg.deleteLater()
    preview_files = sorted(p.name for p in tmp_path.glob("scan*.jpg"))

    assert direct_files == preview_files, \
        f"两条路径的文件名不一致: 直接保存={direct_files} 预览保存={preview_files}"
    assert direct_files == ["scan.jpg"], \
        f"单页不该带序号后缀: {direct_files}"


def test_multi_page_both_paths_use_same_naming(qapp, tmp_path, isolated_cache,
                                               tmp_config_dir, monkeypatch,
                                               no_popups):
    """多页：直接保存与预览保存也都是 {base}_001 / {base}_002"""
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    job_id = "job_i2_multi"
    paths = []
    for i in range(1, 3):
        paths.append(_write_page(job_id, i,
                                  Image.new("RGB", (60, 80), (200, 220, 240))))
    output_path = tmp_path / "multi.jpg"

    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg", str(output_path), 2)
    try:
        dlg._confirm()
        assert _wait_for(lambda: dlg._save_finished, qapp=qapp), "预览保存没结束"
    finally:
        dlg.deleteLater()

    names = sorted(p.name for p in tmp_path.glob("multi*.jpg"))
    assert names == ["multi_001.jpg", "multi_002.jpg"], f"命名不对: {names}"


# ────────── I-3 裁边不许覆盖缓存原件 ──────────

def test_crop_does_not_modify_cache_original(qapp, tmp_path, isolated_cache):
    """裁边后缓存原件的字节必须**一字不变**"""
    cache = tmp_path / "page_001.png"
    _scan_with_border().save(cache, "PNG")
    before = cache.read_bytes()

    dlg = gui.PreviewDialog(None, str(cache), "png", str(tmp_path / "out.png"),
                            "job_i3")
    try:
        dlg._auto_crop()
        after = cache.read_bytes()
        assert before == after, "裁边把缓存原件改了！"
        assert dlg._crop_path and os.path.exists(dlg._crop_path), "没生成裁边件"
        assert dlg._crop_path != str(cache), "裁边件不能就是原件"
    finally:
        dlg.deleteLater()


def test_crop_output_format_matches_extension(qapp, tmp_path, isolated_cache):
    """裁边件的格式必须与扩展名一致（.png ⇒ PNG，不是 JPEG 字节）"""
    cache = tmp_path / "page_001.png"
    _scan_with_border().save(cache, "PNG")

    dlg = gui.PreviewDialog(None, str(cache), "png", str(tmp_path / "out.png"),
                            "job_i3_fmt")
    try:
        dlg._auto_crop()
        crop_path = dlg._crop_path
        with Image.open(crop_path) as im:
            assert im.format == "PNG", f"PNG 输入产出了 {im.format}"
            assert im.size != (100, 120), "没真的裁掉"
    finally:
        dlg.deleteLater()


def test_crop_output_is_valid_image(qapp, tmp_path, isolated_cache):
    """裁边件必须能被 PIL 打开（没写坏）"""
    cache = tmp_path / "page_001.png"
    _scan_with_border().save(cache, "PNG")
    dlg = gui.PreviewDialog(None, str(cache), "png", str(tmp_path / "out.png"),
                            "job_i3_ok")
    try:
        dlg._auto_crop()
        with Image.open(dlg._crop_path) as im:
            im.verify()
    finally:
        dlg.deleteLater()


# ────────── I-4 裁边/旋转后 page_meta 要跟上 ──────────

def test_page_meta_updated_after_crop(qapp, tmp_path, isolated_cache):
    """裁边后 page_meta 的 width/height/crop_rect 必须与实际一致"""
    job_id = "job_i4"
    cache = _write_page(job_id, 1, _scan_with_border(), ext="png")
    dlg = gui.PreviewDialog(None, cache, "png", str(tmp_path / "out.png"), job_id)
    try:
        dlg._auto_crop()
        store = page_meta.PageMetaStore(cache_manager._cache_root())
        meta = store.get_job(job_id)["1"]
        with Image.open(dlg._crop_path) as im:
            w, h = im.size
        assert (meta["width"], meta["height"]) == (w, h), \
            f"page_meta 尺寸与实际不符: {meta['width']}x{meta['height']} vs {w}x{h}"
        assert meta["crop_rect"], "crop_rect 没写"
        left, top, right, bottom = meta["crop_rect"]
        assert (right - left, bottom - top) == (w, h), "crop_rect 与实际尺寸不符"
        assert meta["cache_path"] == cache, "cache_path 必须仍指向原始扫描件"
    finally:
        dlg.deleteLater()


def test_page_meta_records_manual_rotation(qapp, tmp_path, isolated_cache):
    """手动旋转必须落进 page_meta（不能只在内存里）"""
    job_id = "job_i4_rot"
    cache = _write_page(job_id, 1, _scan_with_border(), ext="png")
    dlg = gui.PreviewDialog(None, cache, "png", str(tmp_path / "out.png"), job_id)
    try:
        dlg._rotate_right()
        store = page_meta.PageMetaStore(cache_manager._cache_root())
        meta = store.get_job(job_id)["1"]
        assert meta["manual_rotation"] == 90, \
            f"manual_rotation 没落盘: {meta.get('manual_rotation')}"
    finally:
        dlg.deleteLater()


# ────────── I-5 分组入口（splits 不能是死代码） ──────────

def test_split_button_creates_real_groups(qapp, tmp_path, isolated_cache):
    """点「＋分组线」⇒ splits 真的被填上 ⇒ _build_groups 产出多组"""
    job_id = "job_i5"
    for i in range(1, 5):
        _write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)))

    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                     str(tmp_path / "multi.jpg"), 4)
    try:
        assert dlg.splits == set(), "初始不该有分割点"
        dlg.selected_idx = 2
        dlg._toggle_split()
        assert dlg.splits == {2}, f"分割点没被记下: {dlg.splits}"
        assert dlg._build_groups() == [[0, 1], [2, 3]], \
            f"分组不对: {dlg._build_groups()}"
        # 再点一次 ⇒ 取消
        dlg._toggle_split()
        assert dlg.splits == set()
        assert dlg._build_groups() == [[0, 1, 2, 3]]
    finally:
        dlg.deleteLater()


def test_first_page_cannot_be_split_point(qapp, tmp_path, isolated_cache):
    """第 1 页不能作为分组起点（它本来就是第一组的开头）"""
    job_id = "job_i5_first"
    for i in range(1, 4):
        _write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)))
    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                     str(tmp_path / "multi.jpg"), 3)
    try:
        dlg.selected_idx = 0
        dlg._toggle_split()
        assert dlg.splits == set()
    finally:
        dlg.deleteLater()


def test_two_splits_pdf_saves_three_files(qapp, tmp_path, isolated_cache,
                                           tmp_config_dir, monkeypatch,
                                           no_popups):
    """插入 2 条分组线 ⇒ 保存产出 3 个 PDF（每组一个），命名按多组规则"""
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    job_id = "job_i5_pdf"
    for i in range(1, 6):
        # 选 PDF 时缓存页的扩展名就是 .pdf（内容仍是图片字节，与真机一致）
        _write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)),
                    ext="pdf")

    dlg = gui.MultiPagePreviewDialog(None, job_id, "pdf",
                                     str(tmp_path / "multi.pdf"), 5)
    try:
        dlg.selected_idx = 2
        dlg._toggle_split()
        dlg.selected_idx = 4
        dlg._toggle_split()
        assert dlg.splits == {2, 4}
        assert dlg._build_groups() == [[0, 1], [2, 3], [4]]
        dlg._confirm()
        assert _wait_for(lambda: dlg._save_finished, qapp=qapp), "保存没结束"
    finally:
        dlg.deleteLater()

    names = sorted(p.name for p in tmp_path.glob("multi*.pdf"))
    assert names == ["multi_001.pdf", "multi_002.pdf", "multi_003.pdf"], \
        f"分组产出不对: {names}"


def test_split_saving_uses_multi_group_naming(qapp, tmp_path, isolated_cache,
                                               tmp_config_dir, monkeypatch,
                                               no_popups):
    """图片格式多组保存 ⇒ 文件名带组号，组间不覆盖"""
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    job_id = "job_i5_jpg"
    for i in range(1, 4):
        _write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)))

    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                     str(tmp_path / "multi.jpg"), 3)
    try:
        dlg.selected_idx = 1
        dlg._toggle_split()
        dlg._confirm()
        assert _wait_for(lambda: dlg._save_finished, qapp=qapp), "保存没结束"
    finally:
        dlg.deleteLater()

    names = sorted(p.name for p in tmp_path.glob("multi*.jpg"))
    expected = ["multi_001_001.jpg", "multi_002_001.jpg", "multi_002_002.jpg"]
    assert names == expected, f"多组命名不对: {names}（期望 {expected}）"


def test_delete_page_moves_split(qapp, tmp_path, isolated_cache, monkeypatch):
    """删掉落在分组线上的页 ⇒ 分组线移除；删掉前面的页 ⇒ 分组线前移"""
    fake = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", fake)
    job_id = "job_i5_del"
    for i in range(1, 6):
        _write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)))

    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                     str(tmp_path / "multi.jpg"), 5)
    try:
        dlg.splits = {2, 4}
        dlg.selected_idx = 2          # 删掉第 3 页（分割点正好落在它身上）
        dlg._delete_current_page()
        assert dlg.splits == {3}, f"分割点没跟着调整: {dlg.splits}"
        flat = [i for g in dlg._build_groups() for i in g]
        assert flat == list(range(len(dlg.pages))), "分组里含被删页"
    finally:
        dlg.deleteLater()


# ────────── I-6 直接保存不许静默套用上一次的曝光 ──────────

def test_new_scan_clears_stale_exposure(monkeypatch, tmp_path, no_popups):
    """
    新扫描开始 ⇒ 上一份文档的曝光被清掉 ⇒ 直接保存不会套用旧参数。

    选的是 brief 里的方案 (c)：每次扫描开始清空 `_exposure_state`
    （曝光是"预览里的临时调整"，跨文档静默套用会让用户扫出来的图被改过）。
    """
    calls = []
    monkeypatch.setattr(
        gui.save_manager, "save_single",
        lambda *a, **k: calls.append(k) or "x.jpg")
    monkeypatch.setattr(
        gui.save_manager, "save_pages",
        lambda *a, **k: calls.append(k) or gui.save_manager.SaveResult(["x.jpg"]))

    app = _StubApp(direct_save=True, exposure={"mode": "manual", "brightness": 80})
    assert app._exposure_state is not None

    # 扫描开始 ⇒ 清残留
    app._reset_exposure_state()
    assert app._exposure_state is None

    app._on_scan_finished("job_i6", _make_result(tmp_path, page_count=1))
    assert calls, "直接保存没调落盘层"
    assert calls[0]["exposure"] is None, \
        f"直接保存套用了残留曝光: {calls[0]['exposure']}"


def test_direct_save_still_applies_exposure_set_in_this_scan(monkeypatch,
                                                              tmp_path, no_popups):
    """本次扫描内设置的曝光仍要生效（不能为了消残留把曝光功能改没了）"""
    calls = []
    monkeypatch.setattr(
        gui.save_manager, "save_single",
        lambda *a, **k: calls.append(k) or "x.jpg")
    monkeypatch.setattr(
        gui.save_manager, "save_pages",
        lambda *a, **k: calls.append(k) or gui.save_manager.SaveResult(["x.jpg"]))

    app = _StubApp(direct_save=True, exposure={"mode": "manual", "brightness": 25})
    app._on_scan_finished("job_i6b", _make_result(tmp_path, page_count=1))
    assert calls[0]["exposure"]["brightness"] == 25


def test_exposure_state_is_per_scan(monkeypatch, tmp_path):
    """set_exposure_state → 新扫描清空 ⇒ 不跨文档残留"""
    app = _StubApp(direct_save=True)
    gui.ScanApp.set_exposure_state(app, {"mode": "manual", "brightness": 80})
    assert app._exposure_state["brightness"] == 80
    app._reset_exposure_state()
    assert app._exposure_state is None


# ────────── M-1 注释里的实测值 ──────────

def test_manual_exposure_comment_matches_measurement():
    """手动曝光主线程耗时的注释必须写实测值 0.130s（不能留 0.022s）"""
    src = Path(gui.__file__).read_text(encoding="utf-8")
    assert "0.022s" not in src, "注释里还留着不准的 0.022s"
    assert "0.130s" in src, "注释里没有实测值 0.130s"


# ────────── F-8 job 必须注销 ──────────

def test_preview_close_releases_active_job(qapp, tmp_path, isolated_cache):
    """预览关闭且未保存 ⇒ job 必须从受保护集合里注销"""
    cache = tmp_path / "page_001.jpg"
    Image.new("RGB", (60, 80), (200, 220, 240)).save(cache, "JPEG")
    cache_manager.register_job("job_f8")
    assert cache_manager.is_job_protected("job_f8")

    dlg = gui.PreviewDialog(None, str(cache), "jpg", str(tmp_path / "out.jpg"),
                            "job_f8")
    dlg.reject()
    assert not cache_manager.is_job_protected("job_f8"), \
        "预览关闭后 job 还受保护 ⇒ 缓存目录永远不被回收"
    dlg.deleteLater()


def test_preview_close_releases_pending_job(qapp, tmp_path, isolated_cache):
    """协调器路径的「已扫描未保存」保护也必须注销"""
    cache = tmp_path / "page_001.jpg"
    Image.new("RGB", (60, 80), (200, 220, 240)).save(cache, "JPEG")
    cache_manager.register_pending_job("job_f8_pending")
    assert cache_manager.is_job_protected("job_f8_pending")

    dlg = gui.PreviewDialog(None, str(cache), "jpg", str(tmp_path / "out.jpg"),
                            "job_f8_pending")
    dlg.reject()
    assert not cache_manager.is_job_protected("job_f8_pending")
    dlg.deleteLater()


def test_multi_preview_close_releases_job(tmp_path, isolated_cache):
    """多页预览关闭 ⇒ 同样注销"""
    job_id = "job_f8_multi"
    for i in range(1, 3):
        _write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)))
    cache_manager.register_job(job_id)

    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                     str(tmp_path / "out.jpg"), 2)
    dlg.reject()
    assert not cache_manager.is_job_protected(job_id)
    dlg.deleteLater()


# ────────── F-5 多页保存不许阻塞主线程 ──────────

def test_multi_page_save_runs_off_main_thread(qapp, tmp_path, isolated_cache,
                                               tmp_config_dir, monkeypatch,
                                               no_popups):
    """落盘必须在后台线程；完成回调必须回主线程"""
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
    save_threads = []
    real_save = save_manager.save_pages

    def _spy(*a, **k):
        save_threads.append(threading.current_thread())
        return real_save(*a, **k)

    monkeypatch.setattr(save_manager, "save_pages", _spy)

    job_id = "job_f5"
    for i in range(1, 3):
        _write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)))

    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                     str(tmp_path / "out.jpg"), 2)
    callback_threads = []
    real_done = dlg._on_save_done

    def _spy_done(files):
        callback_threads.append(threading.current_thread())
        return real_done(files)

    dlg._on_save_done = _spy_done
    try:
        dlg._confirm()
        assert _wait_for(lambda: dlg._save_finished, qapp=qapp), "保存没结束"
    finally:
        dlg.deleteLater()

    assert save_threads, "没调用 save_pages"
    assert save_threads[0] is not threading.main_thread(), \
        "落盘还在主线程（会把界面卡死）"
    assert callback_threads, "没收到完成回调"
    assert callback_threads[0] is threading.main_thread(), \
        "完成回调没回主线程"


def test_multi_page_save_error_returns_to_main_thread(qapp, tmp_path,
                                                      isolated_cache,
                                                      tmp_config_dir,
                                                      monkeypatch, no_popups):
    """保存失败也必须回主线程弹错误"""
    monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(save_manager, "save_pages", _boom)

    job_id = "job_f5_err"
    for i in range(1, 3):
        _write_page(job_id, i, Image.new("RGB", (60, 80), (200, 220, 240)))

    dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg",
                                     str(tmp_path / "out.jpg"), 2)
    threads = []
    real_err = dlg._on_save_error

    def _spy_err(err):
        threads.append(threading.current_thread())
        return real_err(err)

    dlg._on_save_error = _spy_err
    try:
        dlg._confirm()
        assert _wait_for(lambda: dlg._save_finished, qapp=qapp), "失败回调没回来"
    finally:
        dlg.deleteLater()

    assert threads and threads[0] is threading.main_thread(), \
        "失败回调没回主线程"
