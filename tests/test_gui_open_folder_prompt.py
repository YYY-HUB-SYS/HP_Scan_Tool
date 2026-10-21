"""
保存后「打开文件夹」的可配置确认弹窗 —— 回归测试（需求 1）

★ 背景
---------------------------------------------------------------------------
PRD 原稿写「保存完成 → 弹窗『打开文件夹？』」，代码里**从来没有**这个弹窗：
`hp_scan_gui_pyside.py` 保存成功后直接 `os.startfile(dirname(saved_files[0]))`。
本轮把它做成**可配置**：

- 配置项（`config_io` 管的 `scan_config.json`）：
  · `open_folder_prompt`   —— 默认 **False** = 保持现状（直接打开、不弹窗，向后兼容）
  · `open_folder_remember` —— "ask"（默认，每次都问）/ "always" / "never"
  （默认值本体 = `config_io.GUI_PREFERENCE_DEFAULTS`）
- 开启后 ⇒ 保存成功弹**模态**确认框（「已保存：<路径>」「是否打开所在文件夹？」），
  选「是」才 `os.startfile`；框里可勾「记住此选择」⇒ **写回配置** ⇒ 下次不再问。

★ 全部 offscreen 跑：QMessageBox 被替身拦掉、`os.startfile` 被本文件的记录器拦掉
  ⇒ **绝不弹任何窗口**（也不会真的打开资源管理器）。
"""

import io
import os
import tempfile
import types

import pytest
from PIL import Image

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
# 测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_gui_openfolder_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import cache_manager
    import config_io
    import scan_coordinator
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)


@pytest.fixture(scope="module")
def qapp(qt_app):
    """整个模块共用一个 QApplication（offscreen，不显示窗口）"""
    return qt_app


# ────────── QMessageBox 替身（支持**实例**用法）────────────────────────

class _FakeCheckBox:
    def __init__(self):
        self._checked = False

    def isChecked(self):
        return self._checked

    def setChecked(self, value):
        self._checked = bool(value)


class _FakeMessageBox:
    """
    QMessageBox 替身 —— 支持 `_ask_open_folder` 用的**实例**接口
    （构造 + setCheckBoxText + exec + checkBox），绝不弹真实窗口。

    ★ `next_reply` / `next_remember` 是**类级**开关：测试在调用被测代码**之前**改它们，
      被测代码构造实例时就会读到期值。
    """

    Question = 1
    Information = 2
    Warning = 3
    Critical = 4
    Yes = 0x00004000
    No = 0x00010000
    Cancel = 0x00400000

    next_reply = Yes
    next_remember = False
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.title = args[1] if len(args) > 1 else kwargs.get("title", "")
        self.text = args[2] if len(args) > 2 else kwargs.get("text", "")
        self.checkbox_text = ""
        self.default_button = None
        self.exec_calls = 0
        self._checkbox = _FakeCheckBox()
        type(self).instances.append(self)

    # ── 静态弹窗（GUI 其它路径用）──
    def information(self, parent, title, text):
        pass

    def warning(self, parent, title, text):
        pass

    def critical(self, parent, title, text):
        pass

    def question(self, parent, title, text, *a, **k):
        return type(self).next_reply

    # ── 实例接口（`_ask_open_folder` 用）──
    def setCheckBoxText(self, text):
        self.checkbox_text = text

    def setDefaultButton(self, button):
        self.default_button = button

    def checkBox(self):
        return self._checkbox

    def exec(self):
        self.exec_calls += 1
        self._checkbox.setChecked(type(self).next_remember)
        return type(self).next_reply


@pytest.fixture
def fake_box(monkeypatch):
    """把 QMessageBox 换成替身（记录调用，绝不弹窗）。"""
    _FakeMessageBox.instances = []
    _FakeMessageBox.next_reply = _FakeMessageBox.Yes
    _FakeMessageBox.next_remember = False
    monkeypatch.setattr(gui, "QMessageBox", _FakeMessageBox)
    return _FakeMessageBox


@pytest.fixture
def opened(monkeypatch):
    """`os.startfile` 记录器（**不真的打开文件夹**）。"""
    calls = []
    monkeypatch.setattr(gui.os, "startfile",
                        lambda path, *a, **k: calls.append(path))
    return calls


# ────────── 配置读写小工具 ────────────────────────────────────────────

def _write_cfg(**kwargs):
    """把配置写进（隔离的）scan_config.json。"""
    cfg = scan_coordinator.load_config()
    cfg.update(kwargs)
    assert scan_coordinator.save_config(cfg), "测试配置写盘失败"
    return cfg


def _read_cfg():
    return scan_coordinator.load_config()


def _write_page(job_id, page_num=1, ext="jpg"):
    """往隔离缓存里写一页，返回缓存文件路径。"""
    buf = io.BytesIO()
    Image.new("RGB", (60, 80), (200, 220, 240)).save(buf, "JPEG")
    return cache_manager.write_page(job_id, page_num, buf.getvalue(), ext)


# ────────── 默认行为（向后兼容）────────────────────────────────────────

class TestDefaultKeepsLegacyBehavior:

    def test_defaults_are_backward_compatible(self):
        """★ 默认 = 保持现状：直接打开、不弹窗。"""
        assert config_io.GUI_PREFERENCE_DEFAULTS["open_folder_prompt"] is False
        assert config_io.GUI_PREFERENCE_DEFAULTS["open_folder_remember"] == "ask"

    def test_missing_config_key_opens_folder_without_prompt(
            self, tmp_path, fake_box, opened):
        """配置里没有这个键（老用户的配置）⇒ 直接打开，不弹框。"""
        assert gui._open_folder_after_save([str(tmp_path / "a.jpg")]) is True
        assert opened == [str(tmp_path)]
        assert fake_box.instances == [], "没开开关却弹了框"

    def test_prompt_disabled_opens_folder_without_prompt(
            self, tmp_path, fake_box, opened):
        """开关显式为 False ⇒ 同样直接打开、不弹框。"""
        _write_cfg(open_folder_prompt=False)
        assert gui._open_folder_after_save([str(tmp_path / "a.jpg")]) is True
        assert opened == [str(tmp_path)]
        assert fake_box.instances == []


# ────────── 开启后：弹模态确认框 ───────────────────────────────────────

class TestPromptEnabled:

    def test_yes_opens_the_folder(self, tmp_path, fake_box, opened):
        """选「是」⇒ 打开所在文件夹。"""
        _write_cfg(open_folder_prompt=True)
        fake_box.next_reply = fake_box.Yes
        assert gui._open_folder_after_save([str(tmp_path / "a.jpg")]) is True
        assert opened == [str(tmp_path)]
        assert len(fake_box.instances) == 1

    def test_no_does_not_open_the_folder(self, tmp_path, fake_box, opened):
        """选「否」⇒ **不**打开。"""
        _write_cfg(open_folder_prompt=True)
        fake_box.next_reply = fake_box.No
        assert gui._open_folder_after_save([str(tmp_path / "a.jpg")]) is False
        assert opened == []

    def test_dialog_shows_saved_path_and_question(self, tmp_path, fake_box,
                                                  opened):
        """★ 框里必须写清「已保存：<路径>」并问「是否打开所在文件夹？」。"""
        _write_cfg(open_folder_prompt=True)
        gui._open_folder_after_save([str(tmp_path / "a.jpg")])
        box = fake_box.instances[0]
        assert str(tmp_path) in box.text, "框里没写保存路径: %r" % box.text
        assert "是否打开所在文件夹" in box.text, "框里没问要不要打开: %r" % box.text
        assert box.checkbox_text, "框里没有「记住选择」复选框"

    def test_dialog_is_modal(self, tmp_path, fake_box, opened):
        """★ 必须是**模态**对话框（exec() 阻塞到用户回答）。"""
        _write_cfg(open_folder_prompt=True)
        gui._open_folder_after_save([str(tmp_path / "a.jpg")])
        assert fake_box.instances[0].exec_calls == 1


# ────────── 记住选择 ⇒ 写回配置 ────────────────────────────────────────

class TestRememberChoice:

    def test_remember_yes_writes_config_and_skips_next_prompt(
            self, tmp_path, fake_box, opened):
        """★ 勾「记住此选择」+ 选「是」⇒ 写回配置 ⇒ 下次保存不再问、直接打开。"""
        _write_cfg(open_folder_prompt=True)
        fake_box.next_reply = fake_box.Yes
        fake_box.next_remember = True
        gui._open_folder_after_save([str(tmp_path / "a.jpg")])
        assert _read_cfg().get("open_folder_remember") == "always"

        before = len(fake_box.instances)
        assert gui._open_folder_after_save([str(tmp_path / "b.jpg")]) is True
        assert len(fake_box.instances) == before, "记住之后还弹框"
        assert opened == [str(tmp_path), str(tmp_path)]

    def test_remember_no_writes_config_and_never_opens(
            self, tmp_path, fake_box, opened):
        """★ 勾「记住此选择」+ 选「否」⇒ 写回 "never" ⇒ 下次既不问也不打开。"""
        _write_cfg(open_folder_prompt=True)
        fake_box.next_reply = fake_box.No
        fake_box.next_remember = True
        gui._open_folder_after_save([str(tmp_path / "a.jpg")])
        assert _read_cfg().get("open_folder_remember") == "never"

        before = len(fake_box.instances)
        assert gui._open_folder_after_save([str(tmp_path / "b.jpg")]) is False
        assert len(fake_box.instances) == before, "记住之后还弹框"
        assert opened == []

    def test_not_remembered_keeps_asking(self, tmp_path, fake_box, opened):
        """没勾「记住此选择」⇒ 下次**照问**。"""
        _write_cfg(open_folder_prompt=True)
        fake_box.next_remember = False
        gui._open_folder_after_save([str(tmp_path / "a.jpg")])
        gui._open_folder_after_save([str(tmp_path / "b.jpg")])
        assert len(fake_box.instances) == 2, "没勾记住却不再问了"
        assert _read_cfg().get("open_folder_remember", "ask") == "ask"


# ────────── 边界 ────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_no_saved_files_does_nothing(self, tmp_path, fake_box, opened):
        """没产出文件 ⇒ 不弹框、不打开。"""
        _write_cfg(open_folder_prompt=True)
        assert gui._open_folder_after_save([]) is False
        assert opened == []
        assert fake_box.instances == []

    def test_illegal_config_value_falls_back_to_default(self):
        """★ 配置文件被手改坏（类型/取值不对）⇒ 退回默认值，不能崩。"""
        cfg = {"open_folder_prompt": "yes", "open_folder_remember": "bogus"}
        assert config_io.get_gui_preference(cfg, "open_folder_prompt") is False
        assert config_io.get_gui_preference(cfg, "open_folder_remember") == "ask"
        assert config_io.get_gui_preference(None, "open_folder_prompt") is False

    def test_set_gui_preference_reports_change(self):
        cfg = {}
        assert config_io.set_gui_preference(cfg, "open_folder_prompt", True) is True
        assert cfg["open_folder_prompt"] is True
        assert config_io.set_gui_preference(cfg, "open_folder_prompt", True) is False
        assert config_io.set_gui_preference(cfg, "no_such_key", 1) is False


# ────────── 两个保存点都必须走同一套逻辑 ────────────────────────────────

class TestBothSaveSites:

    def test_single_page_preview_save_asks_before_opening(
            self, qapp, tmp_path, isolated_cache, fake_box, opened, monkeypatch):
        """单页预览保存：走同一套「询问后打开」。"""
        job_id = "job_open_folder_single"
        cache_path = _write_page(job_id, 1)
        out = tmp_path / "out.jpg"
        monkeypatch.setattr(gui, "_save_page_specs",
                            lambda *a, **k: [str(out)])
        _write_cfg(open_folder_prompt=True)
        fake_box.next_reply = fake_box.No
        dlg = gui.PreviewDialog(None, cache_path, "jpg", str(out), job_id)
        try:
            dlg._confirm()
        finally:
            dlg.close()
        assert len(fake_box.instances) == 1, "单页保存没弹确认框"
        assert opened == [], "选了「否」却还是打开了文件夹"

    def test_multi_page_save_asks_before_opening(
            self, qapp, tmp_path, isolated_cache, fake_box, opened):
        """多页保存：同样走同一套「询问后打开」。"""
        job_id = "job_open_folder_multi"
        _write_page(job_id, 1)
        _write_page(job_id, 2)
        out = tmp_path / "out.pdf"
        _write_cfg(open_folder_prompt=True)
        fake_box.next_reply = fake_box.Yes
        dlg = gui.MultiPagePreviewDialog(None, job_id, "jpg", str(out), 2)
        try:
            dlg._on_save_done([str(out)])
        finally:
            dlg.close()
        assert len(fake_box.instances) == 1, "多页保存没弹确认框"
        assert opened == [str(tmp_path)]


# ────────── 开关（快捷键 Ctrl+Shift+O）─────────────────────────────────

class _ToggleStub:
    """`_toggle_open_folder_prompt` 的最小替身。"""

    _toggle_open_folder_prompt = gui.ScanApp._toggle_open_folder_prompt

    def __init__(self, cfg=None):
        self.cfg = cfg if cfg is not None else {}
        self.saved = []
        self.messages = []
        self.status_bar = types.SimpleNamespace(showMessage=self.messages.append)

    def _save_cfg(self, cfg=None, what="设置", notify=True):
        self.saved.append(what)
        return True


class TestToggleSwitch:

    def test_toggle_flips_config_and_persists(self):
        """★ 快捷键切换 ⇒ 配置翻转 + 落盘（重启后依然生效）。"""
        stub = _ToggleStub()
        stub._toggle_open_folder_prompt()
        assert stub.cfg["open_folder_prompt"] is True
        assert stub.cfg["open_folder_remember"] == "ask"
        assert stub.saved == ["打开文件夹设置"]
        assert "开启" in stub.messages[-1]

        stub._toggle_open_folder_prompt()
        assert stub.cfg["open_folder_prompt"] is False
        assert stub.saved == ["打开文件夹设置", "打开文件夹设置"]

    def test_toggle_resets_remembered_choice(self):
        """手动切换 ⇒ 「记住的选择」复位成"每次都问"。"""
        stub = _ToggleStub({"open_folder_prompt": True,
                            "open_folder_remember": "never"})
        stub._toggle_open_folder_prompt()
        assert stub.cfg["open_folder_prompt"] is False
        assert stub.cfg["open_folder_remember"] == "ask"
