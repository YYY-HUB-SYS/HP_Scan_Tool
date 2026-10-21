"""
多页预览「重命名分组」—— 回归测试（HP Scan Tool v4.0）

★ 背景
---------------------------------------------------------------------------
`save_manager.build_output_name` 生成的组文件名是**自动**的：
  多组时 PDF 是 `{base}_{组号:03d}.pdf`、图片是 `{base}_{组号:03d}_{组内序号:03d}.{ext}`
  ⇒ 用户给不了组名（`HANDOVER.md` 与 `docs/adr/0004-multipage-custom-grouping.md`
  都登记「重命名分组未实现」）。

本轮实现：
  · `save_manager.sanitize_group_name`（纯 Python，**不 import Qt**）—— 组名净化
  · `save_manager.build_output_name(..., group_name="")` —— 多组时用组名替代组号
  · `save_manager.save_pages(..., group_names=None)` / `SaveRequest.group_names`
  · GUI：独立模态对话框 `SaveGroupNameDialog` + `MultiPagePreviewDialog._confirm` 接线

★ 硬约束（下面的用例把它钉死）
  1. 单组场景文件名**与改造前逐字节一致**（即使传了组名也必须一致）
  2. 组名为空 / 净化后为空 ⇒ 回落到组号 ⇒ 与改造前完全一致
  3. 组名进文件名 ⇒ 必须净化（Windows 非法字符 / 路径分隔符 / 控制字符 /
     结尾的点与空格 / 超长）⇒ 不产生路径穿越
  4. 多页预览的控件树**零改动**（入口是**独立对话框**，不是往预览里塞输入框）

★ 全部 offscreen 跑：**绝不连真机、绝不弹真窗、绝不出纸**。
"""

import os
import tempfile

import pytest
from PIL import Image

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
    import history_manager
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
def isolated(tmp_path, monkeypatch):
    """把缓存根目录与历史文件重定向到临时目录（与 test_save_manager.py 同款）"""
    cache_root = str(tmp_path / "cache")
    os.makedirs(cache_root, exist_ok=True)
    monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: cache_root)
    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: os.path.join(cache_root, "cache_config.json"))
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(cache_manager, "_active_jobs", set())
    monkeypatch.setattr(cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(cache_manager, "_cache_size_cache", None)
    hist = str(tmp_path / "history.json")
    monkeypatch.setattr(history_manager, "_history_file", lambda: hist)
    monkeypatch.setattr(history_manager, "HISTORY_FILE", None)
    return cache_root, hist


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

    def test_pdf_group_names_land_in_filenames(self, tmp_path, src_jpg, isolated):
        base = str(tmp_path / "HP_Scan.pdf")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_pdf_names", "pdf", base, specs,
                                        [[0], [1]], group_names=["发票", "收据"])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_发票.pdf", "HP_Scan_收据.pdf"}
        assert all(os.path.getsize(f) > 0 for f in files)

    def test_image_group_names_land_in_filenames(self, tmp_path, src_jpg, isolated):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_names", "jpg", base, specs,
                                        [[0], [1]], group_names=["发票", ""])
        names = {os.path.basename(f) for f in files}
        # 第 1 组用组名；第 2 组留空 ⇒ 回落到组号 002
        assert names == {"HP_Scan_发票_001.jpg", "HP_Scan_002_001.jpg"}

    def test_without_group_names_names_are_unchanged(self, tmp_path, src_jpg, isolated):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_legacy", "jpg", base, specs,
                                        [[0], [1]])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_001_001.jpg", "HP_Scan_002_001.jpg"}

    def test_single_group_ignores_group_names(self, tmp_path, src_jpg, isolated):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0)]
        files = save_manager.save_pages("job_img_single", "jpg", base, specs,
                                        [[0]], group_names=["发票"])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_001.jpg"}

    def test_group_names_shorter_than_groups_falls_back(self, tmp_path, src_jpg, isolated):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_partial", "jpg", base, specs,
                                        [[0], [1]], group_names=["发票"])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_发票_001.jpg", "HP_Scan_002_001.jpg"}

    def test_illegal_group_name_is_sanitized_on_disk(self, tmp_path, src_jpg, isolated):
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_dirty", "jpg", base, specs,
                                        [[0], [1]], group_names=["发/票", ".."])
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_发票_001.jpg", "HP_Scan_002_001.jpg"}

    def test_run_save_job_passes_group_names_through(self, tmp_path, src_jpg, isolated):
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

    def test_group_names_passed_as_string_is_ignored(self, tmp_path, src_jpg, isolated):
        """★ 类型闸：误传字符串（`"发票"`）⇒ 整组忽略 ⇒ 回落组号，
        而不是把 `"发票"[0]` 当成第 1 组的组名（那会静默错名）。"""
        base = str(tmp_path / "HP_Scan.jpg")
        specs = [(src_jpg, 0), (src_jpg, 0)]
        files = save_manager.save_pages("job_img_str", "jpg", base, specs,
                                        [[0], [1]], group_names="发票")
        names = {os.path.basename(f) for f in files}
        assert names == {"HP_Scan_001_001.jpg", "HP_Scan_002_001.jpg"}


# ────────── GUI 层：SaveGroupNameDialog ──────────

class TestSaveGroupNameDialog:
    """独立模态对话框（offscreen，不显示窗口）"""

    def _make(self, group_count=2, page_counts=(1, 1), base="C:/out/HP_Scan", ext="pdf"):
        return gui.SaveGroupNameDialog(None, group_count, list(page_counts), base, ext)

    def test_default_inputs_are_empty(self, qapp):
        dlg = self._make()
        assert len(dlg._edits) == 2
        assert all(edit.text() == "" for edit in dlg._edits)

    def test_placeholder_hints_group_index(self, qapp):
        dlg = self._make()
        assert "001" in dlg._edits[0].placeholderText()
        assert "002" in dlg._edits[1].placeholderText()

    def test_preview_defaults_to_group_index_form(self, qapp):
        dlg = self._make()
        assert dlg._previews[0].text() == "C:/out/HP_Scan_001.pdf"
        assert dlg._previews[1].text() == "C:/out/HP_Scan_002.pdf"

    def test_preview_updates_with_input(self, qapp):
        dlg = self._make()
        dlg._edits[0].setText("发票")
        assert dlg._previews[0].text() == "C:/out/HP_Scan_发票.pdf"
        # 另一组不受影响
        assert dlg._previews[1].text() == "C:/out/HP_Scan_002.pdf"

    def test_preview_sanitizes_illegal_chars(self, qapp):
        dlg = self._make()
        dlg._edits[0].setText("发/票")
        assert dlg._previews[0].text() == "C:/out/HP_Scan_发票.pdf"

    def test_image_preview_contains_group_seq(self, qapp):
        dlg = self._make(ext="jpg")
        assert dlg._previews[0].text() == "C:/out/HP_Scan_001_001.jpg"
        dlg._edits[0].setText("发票")
        assert dlg._previews[0].text() == "C:/out/HP_Scan_发票_001.jpg"

    def test_group_names_returns_sanitized_values(self, qapp):
        dlg = self._make()
        dlg._edits[0].setText("  发票  ")
        dlg._edits[1].setText("")
        assert dlg.group_names() == ["发票", ""]

    def test_save_button_accepts_and_returns_names(self, qapp):
        dlg = self._make()
        dlg._edits[0].setText("发票")
        dlg._edits[1].setText("收据")
        dlg.btn_save.click()
        assert dlg.result() == gui.QDialog.Accepted
        assert dlg.group_names() == ["发票", "收据"]

    def test_cancel_button_rejects_and_clears_names(self, qapp):
        dlg = self._make()
        dlg._edits[0].setText("发票")
        dlg.btn_cancel.click()
        assert dlg.result() == gui.QDialog.Rejected
        # 取消 ⇒ 组名作废（接线层直接放弃保存）
        assert dlg.group_names() == ["", ""]

    def test_page_counts_are_shown(self, qapp):
        dlg = self._make(group_count=2, page_counts=(3, 5))
        assert dlg._labels[0].text() == "第 1 组（共 3 页）"
        assert dlg._labels[1].text() == "第 2 组（共 5 页）"

    def test_headless_exec_auto_accepts(self, qapp):
        """★ 无头环境（offscreen）下 `exec()` 必须**自动返回**。

        否则自动化流程会被这个模态框**永久挂死**（多组保存路径）。
        自动接受 ⇒ 组名留空 ⇒ 落盘回落到组号（与改造前一致）。
        """
        dlg = self._make()
        try:
            assert dlg.exec() == gui.QDialog.Accepted
            assert dlg.group_names() == ["", ""]
        finally:
            dlg.deleteLater()


# ────────── GUI 层：_confirm 接线 ──────────

class _FakeGroupNameDialog:
    """SaveGroupNameDialog 替身：记录调用、**绝不弹窗**。"""

    calls = []
    names = []
    events = []
    exec_result = None

    def __init__(self, parent, group_count, page_counts, base_name, ext):
        self.parent = parent
        self.group_count = group_count
        self.page_counts = list(page_counts)
        self.base_name = base_name
        self.ext = ext
        type(self).calls.append(self)
        type(self).events.append("dialog")

    def exec(self):
        return type(self).exec_result

    def group_names(self):
        return list(type(self).names)


class _SaveProbe:
    """拦截落盘入口：记录 SaveRequest 与调用顺序（**绝不真写盘**）。"""

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
def fake_group_dialog(monkeypatch):
    _FakeGroupNameDialog.calls = []
    _FakeGroupNameDialog.names = []
    _FakeGroupNameDialog.events = []
    _FakeGroupNameDialog.exec_result = gui.QDialog.Accepted
    monkeypatch.setattr(gui, "SaveGroupNameDialog", _FakeGroupNameDialog)
    return _FakeGroupNameDialog


@pytest.fixture
def save_probe(monkeypatch):
    probe = _SaveProbe()
    monkeypatch.setattr(save_manager, "run_save_job", probe.run_save_job)
    monkeypatch.setattr(gui, "_run_in_background", probe.run_in_background)
    # 绝不真的打开文件夹 / 弹消息框
    monkeypatch.setattr(gui, "_open_folder_after_save", lambda *a, **k: False)
    return probe


class TestConfirmWiring:
    """`MultiPagePreviewDialog._confirm` 的接线"""

    def _preview(self, tmp_path, page_count=2, splits=None):
        out = str(tmp_path / "HP_Scan.jpg")
        dlg = gui.MultiPagePreviewDialog(None, "job_wiring", "jpg", out, page_count)
        dlg.splits = set(splits or ())
        return dlg

    def test_multi_group_opens_dialog_and_passes_names(
            self, qapp, tmp_path, isolated, fake_group_dialog, save_probe):
        fake_group_dialog.names = ["发票", "收据"]
        dlg = self._preview(tmp_path, splits={1})
        dlg._confirm()
        assert len(fake_group_dialog.calls) == 1
        call = fake_group_dialog.calls[0]
        assert call.group_count == 2
        assert call.page_counts == [1, 1]
        assert call.base_name == str(tmp_path / "HP_Scan")
        assert call.ext == "jpg"
        assert call.parent is dlg
        assert len(save_probe.requests) == 1
        assert save_probe.requests[0].group_names == ["发票", "收据"]

    def test_dialog_opens_before_saving(
            self, qapp, tmp_path, isolated, fake_group_dialog, save_probe):
        """★ 弹窗必须**先于**落盘（否则用户还没起名就已经开始写了）"""
        fake_group_dialog.events = save_probe.events
        dlg = self._preview(tmp_path, splits={1})
        dlg._confirm()
        assert save_probe.events == ["dialog", "save"]

    def test_cancel_does_not_save_and_keeps_state(
            self, qapp, tmp_path, isolated, fake_group_dialog, save_probe):
        fake_group_dialog.exec_result = gui.QDialog.Rejected
        dlg = self._preview(tmp_path, splits={1})
        dlg._confirm()
        assert len(fake_group_dialog.calls) == 1
        assert save_probe.requests == []
        # ★ 取消后状态完全不变（按钮不会卡在「保存中...」）
        assert dlg._saving is False
        assert dlg.save_btn.text() == "保存全部"
        assert dlg.save_btn.isEnabled() is True

    def test_single_group_does_not_open_dialog(
            self, qapp, tmp_path, isolated, fake_group_dialog, save_probe):
        dlg = self._preview(tmp_path, splits=None)
        dlg._confirm()
        assert fake_group_dialog.calls == []
        assert len(save_probe.requests) == 1
        assert save_probe.requests[0].group_names is None

    def test_empty_group_names_still_pass_through(
            self, qapp, tmp_path, isolated, fake_group_dialog, save_probe):
        """全部留空 ⇒ 传空串列表 ⇒ 落盘侧回落到组号（文件名与改造前一致）"""
        fake_group_dialog.names = ["", ""]
        dlg = self._preview(tmp_path, splits={1})
        dlg._confirm()
        assert save_probe.requests[0].group_names == ["", ""]
