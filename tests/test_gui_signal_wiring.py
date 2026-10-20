"""
GUI ↔ 协调器 信号接线守护 — HP Scan Tool v4.0

★ 为什么需要这一组
--------------------------------------------------------------------------
**缺口 1（逻辑链断裂）**：协调器把「配置落盘失败」如实上报
（`config_save_failed` 信号），但 GUI 侧**零接线** ⇒
用户改了分辨率 / 昵称 / 输出路径，界面显示已改，实际**下次启动就没了**，
用户完全不知情 —— 这是「用户以为生效了、其实没生效」那一类缺陷。

**缺口 2（跨模块私有耦合）**：GUI 的 WIA 路径直接写协调器的**私有**
`_cancel_events` 字典 ⇒ 协调器一改字段名就**静默断掉**。
协调器已提供公开 API `register_external_cancel` / `unregister_external_cancel`
（提交 4879437），但 GUI 仍在戳私有字段。

GUI 一律 offscreen 跑（`QT_QPA_PLATFORM=offscreen`），不弹真实窗口。
"""

import os
import re
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hp_scan_gui_pyside as gui  # noqa: E402
import scan_coordinator  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeMessageBox:
    """QMessageBox 替身：记录提示调用，绝不弹窗（否则测试会卡住）"""

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


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    """真实 ScanApp（offscreen）+ 弹窗替身"""
    monkeypatch.setattr(gui, "QMessageBox", _FakeMessageBox())
    app = gui.ScanApp()
    try:
        yield app
    finally:
        try:
            app.close()
        except Exception:
            pass


# ── 缺口 1：config_save_failed 必须被 GUI 接上 ──────────────────────────

class TestConfigSaveFailedWiring:

    def test_signal_reaches_a_user_visible_hint(self, win):
        """★ 协调器报告配置没落盘 ⇒ GUI 必须给出**用户可见**提示。

        不接的后果：用户改了设置，界面显示已改，实际下次启动就丢了。
        """
        win.status_bar.showMessage("")
        win.coordinator.config_save_failed.emit("持久化扫描参数")

        msg = win.status_bar.currentMessage()
        assert msg, "配置落盘失败后状态栏没有任何提示"
        assert "未生效" in msg or "未保存" in msg, (
            "状态栏提示没说明「本次修改未生效」: %r" % msg)

    def test_failure_hint_tells_user_what_to_do(self, win, monkeypatch):
        """提示要含**可执行的下一步**（不是只说"失败了"）。"""
        fake = _FakeMessageBox()
        monkeypatch.setattr(gui, "QMessageBox", fake)
        win.coordinator.config_save_failed.emit("持久化扫描参数")
        assert fake.warning_calls, "配置落盘失败必须弹提示（不能只写日志）"
        text = fake.warning_calls[0][1]
        assert ("重试" in text) or ("占用" in text) or ("关闭" in text), (
            "提示没有给出可执行的下一步: %r" % text)

    def test_empty_context_still_shows_something(self, win):
        """上下文为空也不能静默（否则又变成"什么都没发生"）。"""
        win.status_bar.showMessage("")
        win.coordinator.config_save_failed.emit("")
        assert win.status_bar.currentMessage(), "空上下文时必须仍有提示"


class TestConfigSaveFailureVisibility:
    """
    ★ 光"接了信号"不够 —— 必须确认**用户改设置的每条主路径**都能看见失败。

    协调器的 `config_save_failed` 只在协调器自己的 4 个调用点发出
    （load_config 补缺省值 / set_custom_name / 发现后持久化 / persist_scan_settings），
    而 GUI 的主配置写入走的是 `ScanApp._save_cfg`（自己实现）⇒
    信号接上了，主路径却是**死线**。
    """

    def test_gui_settings_write_failure_is_visible_to_user(self, win, monkeypatch):
        """★ 用户改分辨率/昵称时配置写失败 ⇒ 必须**弹窗**（状态栏用户看不见）。"""
        fake = _FakeMessageBox()
        monkeypatch.setattr(gui, "QMessageBox", fake)
        monkeypatch.setattr(gui, "save_config", lambda cfg: False)

        assert win._save_cfg(what="扫描参数") is False
        assert fake.warning_calls, (
            "GUI 主路径的配置写失败没有弹窗 ⇒ 用户以为改好了，实际没落盘")

    def test_rename_failure_shows_exactly_one_dialog(self, win, monkeypatch):
        """★ 改名失败 ⇒ **恰好一次**弹窗。

        `_save_cfg` 与改名路径都会报失败 ⇒ 若两处各弹一次，用户要点两次"确定"。
        """
        import types
        fake = _FakeMessageBox()
        monkeypatch.setattr(gui, "QMessageBox", fake)
        monkeypatch.setattr(gui, "save_config", lambda cfg: False)

        scanner = types.SimpleNamespace(ip="192.168.1.9", custom_name="",
                                        model="HP Test", has_adf=False)
        win._apply_custom_name(scanner, "新名字")
        assert len(fake.warning_calls) == 1, (
            "改名失败弹了 %d 次窗（应恰好 1 次）: %r"
            % (len(fake.warning_calls), fake.warning_calls))
        assert not any("已重命名" in m for m in [win.status_bar.currentMessage()]), \
            "改名失败不许说已重命名"


# ── 缺口 2：GUI 不得直写协调器私有取消表 ────────────────────────────────

class TestExternalCancelApiUsage:

    def test_gui_source_does_not_touch_private_cancel_table(self):
        """★ GUI 源码里不得再出现 `coordinator._cancel_events` / `_cancel_mutex`。

        直写私有字典是跨模块私有耦合 —— 协调器一改字段名就静默断掉，
        而"取消"断了用户只会看到"点取消没反应"。
        """
        with open(gui.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        offenders = []
        for pat in (r"coordinator\._cancel_events", r"coordinator\._cancel_mutex",
                    r"coordinator\._clear_cancel_event"):
            if re.search(pat, source):
                offenders.append(pat)
        assert offenders == [], (
            "GUI 仍在直写协调器私有成员 %s ⇒ 改走公开 API "
            "register_external_cancel / unregister_external_cancel" % offenders)

    def test_register_then_cancel_scan_reaches_the_event(self, win):
        """走公开 API 登记的取消事件，必须能被 `cancel_scan(job_id)` 置位。"""
        ev = threading.Event()
        job_id = "job_wia_cancel_api"
        assert win._register_cancel_event(job_id, ev) is True
        try:
            win.coordinator.cancel_scan(job_id)
            assert ev.is_set(), "公开 API 登记的取消事件没被 cancel_scan 置位"
        finally:
            win._clear_cancel_event(job_id)

    def test_clear_removes_the_registration(self, win):
        """注销后不该再残留条目（残留 ⇒ shutdown 时会被误置位）。"""
        from PySide6.QtCore import QMutexLocker
        ev = threading.Event()
        job_id = "job_wia_clear_api"
        win._register_cancel_event(job_id, ev)
        win._clear_cancel_event(job_id)
        with QMutexLocker(win.coordinator._cancel_mutex):      # 只读断言内部状态
            assert job_id not in win.coordinator._cancel_events
        # 注销后再取消，不应再置位那个事件
        ev.clear()
        win.coordinator.cancel_scan(job_id)
        assert not ev.is_set(), "注销后 cancel_scan 仍在置位该事件"
