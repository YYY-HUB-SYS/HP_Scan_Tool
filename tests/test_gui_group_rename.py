"""
多页预览「重命名分组」—— 回归测试（HP Scan Tool v4.0）

★ 背景
---------------------------------------------------------------------------
`save_manager.build_output_name` 生成的组文件名是**自动**的：
  多组时 PDF 是 `{base}_{组号:03d}.pdf`、图片是 `{base}_{组号:03d}_{组内序号:03d}.{ext}`
  ⇒ 用户给不了组名。

★ 本轮（2026-10-21 第二版）：入口从「保存前弹独立模态窗口」改成
  **多页预览里每组一个组名输入框**
  · 删掉了 `SaveGroupNameDialog`（独立模态对话框）—— 保存前**不再有任何弹窗**
  · `MultiPagePreviewDialog.group_names`：**组号（1 基）→ 组名**，唯一真源
  · 组名输入框放在该组**第一页的缩略图之前**（第 1 组没有分隔线 ⇒ 放在第 1 个缩略图前）
  · 保存时按组顺序收集进 `SaveRequest.group_names`

★ 硬约束（下面的用例把它钉死）
  1. 单组场景文件名**与改造前逐字节一致**（只有 1 个组名输入框）
  2. 组名为空 / 净化后为空 ⇒ 回落到组号 ⇒ 与改造前完全一致
  3. 组名进文件名 ⇒ 必须净化（落盘侧 `save_manager.sanitize_group_name` 负责）
  4. 组结构变化（切分 / 删页）留下的**过期**组号条目 ⇒ 忽略，不报错
  5. 保存前**不弹任何对话框**

★ 全部 offscreen 跑：**绝不连真机、绝不弹真窗、绝不出纸**。
"""

import io
import os
import tempfile
import time

import pytest
from PIL import Image
from PySide6.QtCore import QEvent

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_gui_group_rename_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import cache_manager
    import save_manager
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)


@pytest.fixture(scope="module")
def qapp(qt_app):
    """整个模块共用一个 QApplication（offscreen，不显示窗口）"""
    return qt_app


@pytest.fixture
def src_jpg(tmp_path):
    """一张真实的小 JPEG（落盘测试用真写文件）"""
    p = tmp_path / "src.jpg"
    Image.new("RGB", (100, 200), "white").save(p, "JPEG")
    return str(p)


# ────────── 纯函数层：sanitize_group_name ──────────

class TestSanitizeGroupName:
    """组名净化（Windows 文件名口径）"""

    def test_illegal_chars_are_deleted(self):
        assert save_manager.sanitize_group_name('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"

    def test_path_separators_are_deleted(self):
        out = save_manager.sanitize_group_name("sub/dir\\name")
        assert "/" not in out and "\\" not in out
        assert out == "subdirname"

    def test_path_traversal_input_stays_one_component(self):
        """`../../etc/passwd` 净化后不得含分隔符、不得是 `..` 组件"""
        out = save_manager.sanitize_group_name("../../etc/passwd")
        assert "/" not in out and "\\" not in out
        assert out not in ("..", ".")
        assert os.path.basename(out) == out

    def test_only_dots_falls_back_to_empty(self):
        assert save_manager.sanitize_group_name("..") == ""
        assert save_manager.sanitize_group_name("...") == ""

    def test_control_chars_are_deleted(self):
        assert save_manager.sanitize_group_name("a\x00b\x1fc\x7fd") == "abcd"

    def test_whitespace_only_is_empty(self):
        assert save_manager.sanitize_group_name("   ") == ""
        assert save_manager.sanitize_group_name("\t\t") == ""

    def test_none_is_empty(self):
        assert save_manager.sanitize_group_name(None) == ""

    def test_non_string_is_empty(self):
        assert save_manager.sanitize_group_name(123) == ""
        assert save_manager.sanitize_group_name(["a"]) == ""
        assert save_manager.sanitize_group_name({"a": 1}) == ""

    def test_empty_string_is_empty(self):
        assert save_manager.sanitize_group_name("") == ""

    def test_trailing_dot_and_space_are_removed(self):
        assert save_manager.sanitize_group_name("报告. ") == "报告"
        assert save_manager.sanitize_group_name("abc... ") == "abc"
        assert save_manager.sanitize_group_name("abc   ") == "abc"

    def test_leading_and_trailing_whitespace_stripped(self):
        assert save_manager.sanitize_group_name("  名字  ") == "名字"

    def test_too_long_is_truncated(self):
        out = save_manager.sanitize_group_name("x" * 200)
        assert len(out) == save_manager.GROUP_NAME_MAX_LEN

    def test_truncation_does_not_leave_trailing_dot(self):
        out = save_manager.sanitize_group_name("y" * 59 + "." + "z" * 20)
        assert len(out) <= save_manager.GROUP_NAME_MAX_LEN
        assert not out.endswith(".")

    def test_chinese_is_preserved(self):
        assert save_manager.sanitize_group_name("发票") == "发票"

    def test_internal_space_is_preserved(self):
        assert save_manager.sanitize_group_name("my group name") == "my group name"

    def test_internal_dot_is_preserved(self):
        assert save_manager.sanitize_group_name("2024.05 发票") == "2024.05 发票"

    def test_sanitize_is_idempotent(self):
        for raw in ("发票", "  a/b  ", "..", "x" * 100, "  ", "报告. "):
            once = save_manager.sanitize_group_name(raw)
            assert save_manager.sanitize_group_name(once) == once

    def test_length_limit_boundary_kept(self):
        raw = "x" * save_manager.GROUP_NAME_MAX_LEN
        assert save_manager.sanitize_group_name(raw) == raw


# ────────── 纯函数层：build_output_name ──────────

class TestBuildOutputName:
    """组名只影响多组；单组必须与改造前逐字节一致"""

    def test_single_group_pdf_ignores_group_name(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 1, 1, 1, "发票") == "C:/out/HP_Scan.pdf"

    def test_single_group_image_ignores_group_name(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "jpg", 1, 1, 1, "发票") == "C:/out/HP_Scan_001.jpg"

    def test_single_group_zero_count_ignores_group_name(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 0, 1, 1, "发票") == "C:/out/HP_Scan.pdf"

    def test_multi_group_pdf_with_name(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 2, 1, 1, "发票") == "C:/out/HP_Scan_发票.pdf"

    def test_multi_group_pdf_without_name_falls_back_to_index(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 2, 1, 1) == "C:/out/HP_Scan_001.pdf"
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 2, 2, 1) == "C:/out/HP_Scan_002.pdf"

    def test_multi_group_image_with_name(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "jpg", 2, 1, 1, "发票") == "C:/out/HP_Scan_发票_001.jpg"

    def test_multi_group_image_without_name_falls_back_to_index(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "jpg", 2, 1, 1) == "C:/out/HP_Scan_001_001.jpg"
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "jpg", 2, 2, 3) == "C:/out/HP_Scan_002_003.jpg"

    def test_each_group_uses_its_own_name(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 2, 1, 1, "发票") == "C:/out/HP_Scan_发票.pdf"
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 2, 2, 1, "收据") == "C:/out/HP_Scan_收据.pdf"

    def test_illegal_chars_in_name_are_sanitized_first(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 2, 1, 1, "发/票") == "C:/out/HP_Scan_发票.pdf"

    def test_blank_name_falls_back_to_index(self):
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 2, 1, 1, "   ") == "C:/out/HP_Scan_001.pdf"

    def test_name_sanitized_to_empty_falls_back_to_index(self):
        """净化后为空（全是非法字符）⇒ 回落到组号"""
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 2, 1, 1, "///") == "C:/out/HP_Scan_001.pdf"

    def test_legacy_names_are_byte_identical(self):
        """改造前（无组名）的产物名 —— 逐字节钉死"""
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 1, 1, 1) == "C:/out/HP_Scan.pdf"
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "jpg", 1, 1, 1) == "C:/out/HP_Scan_001.jpg"
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "pdf", 2, 1, 1) == "C:/out/HP_Scan_001.pdf"
        assert save_manager.build_output_name(
            "C:/out/HP_Scan", "jpg", 2, 1, 1) == "C:/out/HP_Scan_001_001.jpg"


# ────────── 落盘层：save_pages / run_save_job ──────────

class TestSavePagesWithGroupNames:
    """真写文件（临时目录）⇒ 断言**实际落盘的文件名**"""

    def test_pdf_group_names_land_in_filenames(self, tmp_path, src_jpg):
        base = str(tmp_path / "HP_Scan.pdf")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_pdf_names", "pdf", base, specs,
                                        [[0], [1]], group_names=["发票", "收据"])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_发票.pdf", "HP_Scan_收据.pdf"}
        assert all(os.path.getsize(f) > 0 for f in files)

    def test_image_group_names_land_in_filenames(self, tmp_path, src_jpg):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_names", "jpg", base, specs,
                                        [[0], [1]], group_names=["发票", ""])
        names = {os.path.basename(f) for f in files}
        # 第 1 组用组名；第 2 组留空 ⇒ 回落到组号 002
        assert names == {"HP_Scan_发票_001.jpg", "HP_Scan_002_001.jpg"}

    def test_without_group_names_names_are_unchanged(self, tmp_path, src_jpg):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_legacy", "jpg", base, specs,
                                        [[0], [1]])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_001_001.jpg", "HP_Scan_002_001.jpg"}

    def test_single_group_ignores_group_names(self, tmp_path, src_jpg):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0)]
        files = save_manager.save_pages("job_img_single", "jpg", base, specs,
                                        [[0]], group_names=["发票"])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_001.jpg"}

    def test_group_names_shorter_than_groups_falls_back(self, tmp_path, src_jpg):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_partial", "jpg", base, specs,
                                        [[0], [1]], group_names=["发票"])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_发票_001.jpg", "HP_Scan_002_001.jpg"}

    def test_illegal_group_name_is_sanitized_on_disk(self, tmp_path, src_jpg):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_dirty", "jpg", base, specs,
                                        [[0], [1]], group_names=["发/票", ".."])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_发票_001.jpg", "HP_Scan_002_001.jpg"}

    def test_run_save_job_passes_group_names_through(self, tmp_path, src_jpg):
        base = str(tmp_path / "HP_Scan.pdf")
        req = save_manager.SaveRequest(
            job_id="job_rsj", ext="pdf", output_path=base,
            page_specs=[(src_jpg, 0), (src_jpg, 0)], groups=[[0], [1]],
            group_names=["发票", "收据"])
        files = save_manager.run_save_job(req)
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_发票.pdf", "HP_Scan_收据.pdf"}

    def test_save_request_default_group_names_is_none(self):
        req = save_manager.SaveRequest(job_id="j", ext="pdf", output_path="o.pdf",
                                       page_specs=[], groups=[])
        assert req.group_names is None

    def test_group_names_passed_as_string_is_ignored(self, tmp_path, src_jpg):
        """★ 类型闸：误传字符串（`"发票"`）⇒ 整组忽略 ⇒ 回落组号，
        而不是把 `"发票"[0]` 当成第 1 组的组名（那会静默错名）。"""
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_str", "jpg", base, specs,
                                        [[0], [1]], group_names="发票")
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_001_001.jpg", "HP_Scan_002_001.jpg"}


# ────────── GUI 层：多页预览里的「组名输入框」 ──────────

def _flush(qapp):
    """把 deleteLater 的控件真正销毁掉（否则布局里新旧混在一起）"""
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def _thumbs_widgets(dlg):
    """缩略图列里**当前**的控件（按顺序；跳过 stretch 这类没有 widget 的项）"""
    out = []
    for i in range(dlg.thumbs_layout.count()):
        w = dlg.thumbs_layout.itemAt(i).widget()
        if w is not None:
            out.append(w)
    return out


def _name_edits(dlg):
    """缩略图列里的**组名输入框**（按顺序 ⇒ 下标 0 = 第 1 组）"""
    return [w for w in _thumbs_widgets(dlg) if isinstance(w, gui.QLineEdit)]


def _make_preview(qapp, tmp_path, page_count=2, splits=(), ext="jpg",
                  job_id=None):
    """造一个带**真实缓存页**的多页预览对话框（缩略图能真的出来）"""
    job_id = job_id or ("job_gr_%s_%d" % (ext, page_count))
    fmt = "JPEG" if ext in ("jpg", "jpeg") else "PNG"
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), (200, 220, 240)).save(buf, fmt)
    data = buf.getvalue()
    for page in range(1, page_count + 1):
        cache_manager.write_page(job_id, page, data, ext)
    dlg = gui.MultiPagePreviewDialog(None, job_id, ext,
                                     str(tmp_path / ("out." + ext)), page_count)
    if splits:
        dlg.splits = set(splits)
        dlg._refresh_thumbs()
    _flush(qapp)
    return dlg


class _SaveProbe:
    """拦截落盘入口：记录 SaveRequest（**绝不真写盘**）。"""

    def __init__(self):
        self.requests = []
        self.events = []

    def run_save_job(self, req):
        self.requests.append(req)
        self.events.append("save")
        return save_manager.SaveResult([])

    def run_in_background(self, work, on_done, on_error, label="background"):
        try:
            on_done(work())
        except Exception as e:            # pragma: no cover - 防御
            on_error(e)


@pytest.fixture
def save_probe(monkeypatch):
    probe = _SaveProbe()
    monkeypatch.setattr(save_manager, "run_save_job", probe.run_save_job)
    monkeypatch.setattr(gui, "_run_in_background", probe.run_in_background)
    # 绝不真的打开文件夹 / 弹消息框
    monkeypatch.setattr(gui, "_open_folder_after_save", lambda *a, **k: False)
    return probe


class TestGroupNameInputs:
    """预览里每组一个组名输入框"""

    def test_single_group_has_exactly_one_input(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=3)
        try:
            assert len(_name_edits(dlg)) == 1, "单组时应当只有 1 个组名输入框"
        finally:
            dlg.deleteLater()

    def test_one_input_per_group(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            assert len(_name_edits(dlg)) == 2
            dlg.splits = {1, 2}
            dlg._refresh_thumbs()
            _flush(qapp)
            assert len(_name_edits(dlg)) == 3
        finally:
            dlg.deleteLater()

    def test_first_group_input_is_before_first_thumbnail(self, qapp, tmp_path,
                                                         isolated_cache):
        """第 1 组没有分隔线 ⇒ 输入框就在第 1 个缩略图之前"""
        dlg = _make_preview(qapp, tmp_path, page_count=2)
        try:
            widgets = _thumbs_widgets(dlg)
            assert isinstance(widgets[0], gui.QLineEdit), \
                "第 1 组的输入框不在缩略图列最前面"
            assert isinstance(widgets[1], gui.QPushButton), \
                "第 1 个缩略图没跟在第 1 组输入框后面"
        finally:
            dlg.deleteLater()

    def test_second_group_input_is_after_separator(self, qapp, tmp_path,
                                                    isolated_cache):
        """第 2 组：分隔线 → 组名输入框 → 该页缩略图"""
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            kinds = [type(w).__name__ for w in _thumbs_widgets(dlg)]
            assert kinds == ["QLineEdit", "QPushButton", "QFrame",
                             "QLineEdit", "QPushButton", "QPushButton"], kinds
        finally:
            dlg.deleteLater()

    def test_placeholder_and_tooltip_mention_group_number(self, qapp, tmp_path,
                                                            isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            edits = _name_edits(dlg)
            # placeholder 说明「留空用组号」，tooltip 给出具体的组号
            assert "组名" in edits[0].placeholderText()
            assert "留空" in edits[0].placeholderText()
            assert "第 2 组" in edits[1].placeholderText()
            assert "001" in edits[0].toolTip()
            assert "002" in edits[1].toolTip()
        finally:
            dlg.deleteLater()

    def test_typing_is_recorded_in_group_names(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            edits = _name_edits(dlg)
            edits[1].setText("合同")
            assert dlg.group_names.get(2) == "合同"
            # 别的组没被污染
            assert dlg.group_names.get(1, "") == ""
        finally:
            dlg.deleteLater()

    def test_name_survives_thumb_refresh(self, qapp, tmp_path, isolated_cache):
        """`_refresh_thumbs()` 会重建输入框 ⇒ 名字必须还在（唯一真源是字典）"""
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            _name_edits(dlg)[1].setText("合同")
            dlg._refresh_thumbs()
            _flush(qapp)
            assert _name_edits(dlg)[1].text() == "合同"
            assert dlg.group_names.get(2) == "合同"
        finally:
            dlg.deleteLater()

    def test_name_survives_page_switch(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            _name_edits(dlg)[1].setText("合同")
            dlg._on_thumb_clicked(2)
            _flush(qapp)
            assert _name_edits(dlg)[1].text() == "合同"
        finally:
            dlg.deleteLater()

    def test_split_button_changes_input_count(self, qapp, tmp_path, isolated_cache):
        """走真实按钮路径（＋分组线）⇒ 组数变 ⇒ 输入框数量跟着变"""
        dlg = _make_preview(qapp, tmp_path, page_count=4)
        try:
            assert len(_name_edits(dlg)) == 1
            dlg.selected_idx = 2
            dlg._toggle_split()
            _flush(qapp)
            assert len(_name_edits(dlg)) == 2
            dlg.selected_idx = 3
            dlg._toggle_split()
            _flush(qapp)
            assert len(_name_edits(dlg)) == 3
        finally:
            dlg.deleteLater()

    def test_page_info_shows_group_name(self, qapp, tmp_path, isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            _name_edits(dlg)[1].setText("合同")
            dlg.selected_idx = 1
            dlg._recompute_preview()
            assert "第 2 组（合同）" in dlg.page_info_label.text(), \
                dlg.page_info_label.text()
        finally:
            dlg.deleteLater()

    def test_page_info_without_name_keeps_plain_group_text(self, qapp, tmp_path,
                                                           isolated_cache):
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            dlg.selected_idx = 1
            dlg._recompute_preview()
            text = dlg.page_info_label.text()
            assert "第 2 组" in text and "（" not in text, text
        finally:
            dlg.deleteLater()


class TestNoNamingDialog:
    """★ 保存前**不再弹任何对话框**（旧的 `SaveGroupNameDialog` 已删除）"""

    def test_naming_dialog_class_is_gone(self):
        assert not hasattr(gui, "SaveGroupNameDialog"), \
            "独立命名对话框还在（应改成预览内输入框）"

    def test_confirm_opens_no_dialog(self, qapp, tmp_path, isolated_cache,
                                     monkeypatch, save_probe):
        opened = []

        def _record(self, *a, **k):
            opened.append(self)
            raise AssertionError("保存前弹出了对话框：%r" % (self,))

        monkeypatch.setattr(gui.QDialog, "exec", _record)
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            _name_edits(dlg)[0].setText("发票")
            dlg._confirm()
            assert opened == [], "保存前弹了对话框"
            assert len(save_probe.requests) == 1
        finally:
            dlg.deleteLater()


class TestConfirmGroupNames:
    """`_confirm` 把组名传给落盘层"""

    def test_names_are_passed_in_group_order(self, qapp, tmp_path, isolated_cache,
                                              save_probe):
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            edits = _name_edits(dlg)
            edits[0].setText("发票")
            edits[1].setText("合同")
            dlg._confirm()
            assert len(save_probe.requests) == 1
            assert save_probe.requests[0].group_names == ["发票", "合同"]
        finally:
            dlg.deleteLater()

    def test_empty_name_is_passed_as_empty_string(self, qapp, tmp_path,
                                                   isolated_cache, save_probe):
        """留空 ⇒ 传空串 ⇒ 落盘侧回落到组号（文件名与改造前一致）"""
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            dlg._confirm()
            assert save_probe.requests[0].group_names == ["", ""]
        finally:
            dlg.deleteLater()

    def test_single_group_passes_one_empty_name(self, qapp, tmp_path,
                                                 isolated_cache, save_probe):
        dlg = _make_preview(qapp, tmp_path, page_count=2)
        try:
            assert len(_name_edits(dlg)) == 1
            dlg._confirm()
            assert save_probe.requests[0].group_names == [""]
        finally:
            dlg.deleteLater()

    def test_stale_group_entries_are_ignored(self, qapp, tmp_path, isolated_cache,
                                              save_probe):
        """组结构变化后 `group_names` 里可能留下**过期**条目 ⇒ 忽略，不报错"""
        dlg = _make_preview(qapp, tmp_path, page_count=3, splits={1})
        try:
            _name_edits(dlg)[1].setText("合同")
            assert dlg.group_names.get(2) == "合同"
            dlg.splits = set()               # 第 2 组没了
            dlg._refresh_thumbs()
            _flush(qapp)
            dlg._confirm()
            assert save_probe.requests[0].group_names == [""]
        finally:
            dlg.deleteLater()


class TestGroupNameReachesFileName:
    """端到端：输入框里起的名字 ⇒ 真的落进产物文件名"""

    def test_group_name_lands_in_output_filename(self, qapp, tmp_path,
                                                  isolated_cache, monkeypatch):
        monkeypatch.setattr(gui.os, "startfile", lambda *a, **k: None)
        dlg = _make_preview(qapp, tmp_path, page_count=2, splits={1})
        try:
            edits = _name_edits(dlg)
            edits[0].setText("发票")
            edits[1].setText("收据")
            dlg._confirm()
            deadline = time.time() + 15.0
            while time.time() < deadline and not dlg._save_finished:
                qapp.processEvents()
                time.sleep(0.01)
            assert dlg._save_finished, "保存没结束"
        finally:
            dlg.deleteLater()

        names = sorted(p.name for p in tmp_path.glob("out*.jpg"))
        assert names == ["out_发票_001.jpg", "out_收据_001.jpg"], names
