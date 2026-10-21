"""
GUI 开放**并发扫描**（多台并行）—— 回归测试（需求 2）

★ 背景
---------------------------------------------------------------------------
后端 `scan_coordinator` 早就用 `QThreadPool`（`setMaxThreadCount(4)`）跑任务，
但 GUI 扫描期间 `scan_btn.setEnabled(False)`、且只跟踪单个 `_current_job_id`
⇒ **一次只能扫一台**（`HANDOVER.md` §4.1「后端支持 / GUI 未开放」）。

本轮把 GUI 侧开放：
- 扫描期间**不再禁用**扫描按钮（可以接着扫下一台）；
- 在途 job 改成**表**（job_id → 该 job 的设备）⇒ 每台各自取消、各自回填结果。

★ 例外（保守起见）：**双面/续扫（会话）模式仍然串行** —— 会话本质是顺序的
  （正面→背面、逐批追加），两批同时入会话会把记账搞乱。

★ 全部 offscreen 跑：QMessageBox 被替身拦掉、扫描协调器被假实现替换
  ⇒ **绝不连真机、绝不弹窗、绝不出纸**。
"""

import os
import tempfile

import pytest

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_gui_concurrent_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import cache_manager
    from scan_coordinator import ScannerInfo
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)


@pytest.fixture(scope="module")
def qapp(qt_app):
    """整个模块共用一个 QApplication（offscreen，不显示窗口）"""
    return qt_app


class _FakeMessageBox:
    """QMessageBox 替身：记录提示调用，绝不弹窗。"""

    Yes = 0x00004000
    No = 0x00010000

    def __init__(self):
        self.critical_calls = []
        self.information_calls = []
        self.warning_calls = []

    def critical(self, parent, title, text):
        self.critical_calls.append((title, text))

    def information(self, parent, title, text):
        self.information_calls.append((title, text))

    def warning(self, parent, title, text):
        self.warning_calls.append((title, text))

    def question(self, parent, title, text, *a, **k):
        return self.Yes


class _FakeResult:
    """扫描结果替身（只带 GUI 用得到的字段）。"""

    def __init__(self, job_id, tmp_path, page_count=1):
        self.job_id = job_id
        self.ext = "jpg"
        self.output_path = str(tmp_path / ("%s.jpg" % job_id))
        self.cached_paths = [
            str(tmp_path / ("%s_%03d.jpg" % (job_id, i)))
            for i in range(1, page_count + 1)
        ]
        self.page_count = page_count


@pytest.fixture
def win(qapp, tmp_path, isolated_cache, monkeypatch):
    """真实 ScanApp（offscreen）+ 假协调器（绝不连真机）。"""
    box = _FakeMessageBox()
    monkeypatch.setattr(gui, "QMessageBox", box)

    app = gui.ScanApp()
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    app.dir_entry.setText(str(out_dir))

    # 拦住启动流程（设备探测 / 恢复会话）
    monkeypatch.setattr(gui.ScanApp, "_finish_loading", lambda self: None)

    # ── 假协调器：start_scan 返回递进的 job id，并记录被扫的设备 ──
    started = []
    cancelled = []

    def _fake_start_scan(scanner, params):
        job_id = "job_%03d" % (len(started) + 1)
        started.append((job_id, scanner, dict(params)))
        return job_id

    def _fake_cancel_scan(job_id):
        cancelled.append(job_id)

    monkeypatch.setattr(app.coordinator, "start_scan", _fake_start_scan)
    monkeypatch.setattr(app.coordinator, "cancel_scan", _fake_cancel_scan)

    # 预览对话框替身（记下**用的是哪台设备**）
    previews = []

    def _fake_preview(self, *args, **kwargs):
        previews.append(args)

    def _fake_multi_preview(self, *args, **kwargs):
        previews.append(args)

    monkeypatch.setattr(gui.ScanApp, "_show_preview", _fake_preview)
    monkeypatch.setattr(gui.ScanApp, "_show_multi_preview", _fake_multi_preview)

    app.started = started
    app.cancelled = cancelled
    app.previews = previews
    app.out_dir = out_dir
    try:
        yield app
    finally:
        try:
            app.close()
        except Exception:
            pass


def _scanner(name, ip):
    return ScannerInfo(name=name, ip=ip, model="HP " + name)


def _result(win, job_id, page_count=1):
    return _FakeResult(job_id, win.out_dir, page_count=page_count)


# ────────── 扫描期间按钮不再禁用 ────────────────────────────────────────

class TestScanButtonStaysUsable:

    def test_scan_button_enabled_while_scanning(self, win):
        """★ 扫描期间扫描按钮**保持可用**（并发扫描已开放）。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        assert win.scan_btn.isEnabled(), "扫描期间扫描按钮被禁用了"
        assert win.scan_btn.text() == "扫描"

    def test_second_scan_can_be_started_while_first_running(self, win):
        """★ A 台还在扫 ⇒ 可以接着扫 B 台（两个在途 job）。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        win.selected = _scanner("B", "192.168.1.2")
        win._start_scan()
        assert len(win.started) == 2
        assert len(win._active_jobs) == 2, "只跟踪了一个 job（还是单 job 模型）"
        assert win.scan_btn.isEnabled()

    def test_only_one_cancel_button_in_status_bar(self, win):
        """状态栏里**只放一个**取消按钮（不能每扫一台塞一个）。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        btn = win.cancel_scan_btn
        win._start_scan()
        assert win.cancel_scan_btn is btn

    def test_progress_message_identifies_device_when_parallel(self, win):
        """多台并行时进度消息必须带设备名（否则看不出是哪台）。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        win.selected = _scanner("B", "192.168.1.2")
        win._start_scan()
        win._on_scan_progress("job_001", "已扫描 3 页", 3)
        msg = win.status_bar.currentMessage()
        assert "已扫描 3 页" in msg
        assert "HP A" in msg, "并行时消息没标出设备: %r" % msg


# ────────── 每台各自回填、各自取消 ──────────────────────────────────────

class TestPerJobTracking:

    def test_each_job_fills_back_with_its_own_scanner(self, win):
        """★ 每台的结果用**它自己的设备**回填（不是当前选中的那台）。"""
        scanner_a = _scanner("A", "192.168.1.1")
        scanner_b = _scanner("B", "192.168.1.2")
        win.selected = scanner_a
        win._start_scan()                    # job_001 → A
        win.selected = scanner_b
        win._start_scan()                    # job_002 → B

        win._on_scan_finished("job_001", _result(win, "job_001"))
        assert win.previews, "job_001 完成却没有回填预览"
        assert win.previews[0][4] is scanner_a, "job_001 用了别的设备的资料"

        win._on_scan_finished("job_002", _result(win, "job_002"))
        assert win.previews[1][4] is scanner_b, "job_002 用了别的设备的资料"

    def test_finishing_one_job_keeps_the_other_scanning(self, win):
        """★ 一台结束**不能**把另一台的界面清掉（进度条/取消按钮要留着）。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        win.selected = _scanner("B", "192.168.1.2")
        win._start_scan()

        win._on_scan_finished("job_001", _result(win, "job_001"))
        assert len(win._active_jobs) == 1, "另一台被误判成结束了"
        assert not win.scan_progress.isHidden(), "另一台还在扫，进度条却收了"
        assert win.cancel_scan_btn is not None, "另一台还在扫，取消按钮却没了"
        assert win.scan_btn.isEnabled()

    def test_cancel_button_cancels_latest_job_only(self, win):
        """★ 取消按钮取消**最近启动**的那台，另一台不受影响。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()                    # job_001
        win.selected = _scanner("B", "192.168.1.2")
        win._start_scan()                    # job_002

        win._cancel_scan()
        assert win.cancelled == ["job_002"], "取消的不是最近启动的那台"
        assert "job_001" in win._active_jobs, "另一台被一起取消了"
        assert not win.scan_progress.isHidden()

    def test_each_job_can_be_cancelled_one_by_one(self, win):
        """★ 每台都能单独取消（点一次取消一台）。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        win.selected = _scanner("B", "192.168.1.2")
        win._start_scan()

        win._cancel_scan()
        win._cancel_scan()
        assert win.cancelled == ["job_002", "job_001"]
        assert win._active_jobs == {}

    def test_all_done_restores_ui(self, win):
        """★ 全部结束 ⇒ 按钮可用、进度条收起、取消按钮移除。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        win.selected = _scanner("B", "192.168.1.2")
        win._start_scan()
        win._on_scan_finished("job_001", _result(win, "job_001"))
        win._on_scan_finished("job_002", _result(win, "job_002"))
        assert win._active_jobs == {}
        assert win.scan_btn.isEnabled()
        assert win.scan_btn.text() == "扫描"
        assert win.scan_progress.isHidden()
        assert win.cancel_scan_btn is None


# ────────── 单台行为必须与改造前一致 ────────────────────────────────────

class TestSingleScanCompatibility:

    def test_single_scan_flow_unchanged(self, win):
        """★ 一台时：扫完 ⇒ 按钮可用、进度条收起、取消按钮移除。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        assert win.scan_btn.isEnabled()
        assert win.scan_progress.isHidden() is False
        win._on_scan_finished("job_001", _result(win, "job_001"))
        assert win.scan_btn.isEnabled()
        assert win.scan_btn.text() == "扫描"
        assert win.scan_progress.isHidden()
        assert win.cancel_scan_btn is None
        assert len(win.previews) == 1

    def test_single_scan_cancel_resets_ui(self, win):
        """★ 一台时取消 ⇒ 界面复位（与改造前一致）。"""
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        win._cancel_scan()
        assert win.cancelled == ["job_001"]
        assert win.scan_btn.isEnabled()
        assert win.scan_progress.isHidden()
        assert win.cancel_scan_btn is None


# ────────── 会话模式（双面/续扫）必须保持串行 ────────────────────────────

class TestSessionModeStaysSerial:

    def test_session_mode_disables_scan_button(self, win):
        """★ 双面/续扫模式下扫描按钮**禁用**（会话本质是顺序的）。"""
        win.mode_batch_radio.setChecked(True)
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        assert not win.scan_btn.isEnabled(), "会话模式下扫描按钮没禁用"
        assert win.scan_btn.text() == "扫描中..."

    def test_session_mode_refuses_second_scan(self, win):
        """★ 会话模式下不允许开第二批（否则正面/背面记账会乱）。"""
        win.mode_batch_radio.setChecked(True)
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()
        win._start_scan()
        assert len(win.started) == 1, "会话模式下开了第二批"
        assert len(win._active_jobs) == 1
        assert "等本次扫描结束" in win.status_bar.currentMessage()


# ────────── 独立复审发现的三条（2026-10-21 修）────────────────────────

class TestSessionGuardCannotBeBypassed:
    """会话（双面 / 续扫）必须单台串行 —— 守卫不能被「扫描中切换模式」绕过。"""

    def test_switching_mode_to_single_does_not_bypass_the_guard(self, win):
        """★ 已有会话扫描在途 ⇒ 切回单面**也不许**再开一台。

        旧实现只看「当前单选框」⇒ 用户在扫描进行中把模式从「续扫」切回「单面」，
        守卫就会放行第二台 ⇒ 变成「会话 + 单面」混合 ⇒ 页面记账错乱。
        """
        win.mode_batch_radio.setChecked(True)          # 续扫（会话）模式
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()                              # job_001 → 会话
        assert len(win.started) == 1

        win.mode_single_radio.setChecked(True)         # ★ 扫描进行中切回单面
        win.selected = _scanner("B", "192.168.1.2")
        win._start_scan()                              # ⇒ 必须被拦住
        assert len(win.started) == 1, "守卫被『切换模式』绕过了（会话扫描不再串行）"
        assert len(win._active_jobs) == 1


class TestSessionRoutingUsesTheJobsOwnMode:
    """页面归属必须按**该 job 自己的模式**，不是当前单选框。"""

    def test_session_job_stays_in_session_when_mode_switches(
            self, win, monkeypatch):
        """★ 会话（续扫）启动的 job，扫描中切到「单面」也**必须**留在会话里。

        旧实现用「当前单选框」判定 ⇒ 页面**从会话里漏出去**（走了预览）
        ⇒ 用户以为并进了文档，实际单独弹了预览。

        ★ 断言方向很关键：必须是「会话 job ⇒ 进会话」。
          反过来测（单面 job ⇒ 不进会话）**测不出东西** ——
          单面模式根本不建会话（`_session` 是 None）⇒ 旧行为在那条路径上
          本来就不会走会话分支 ⇒ 断言恒成立（实测漏过变异）。
        ★ 断言也不能只看 `previews` 非空：会话分支最后也会弹预览。
          ⇒ 直接断言「会话路由被调用了」。
        """
        routed = []
        monkeypatch.setattr(gui.ScanApp, "_session_batch_finished",
                            lambda self, *a, **k: routed.append(a))
        win.mode_batch_radio.setChecked(True)          # 续扫（会话）模式
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()                              # job_001 → 会话
        win.mode_single_radio.setChecked(True)         # ★ 扫描进行中切到单面
        win._on_scan_finished("job_001", _result(win, "job_001"))
        assert routed, "会话 job 的页面被漏出去了（没走会话路由）"


class TestListenUiIsNotBlockedByARunningScan:
    """监听结束的复位不能被「还有扫描在途」挡住（独立复审发现的回归）。"""

    def test_listen_finish_reenables_listen_button_while_scan_runs(self, win):
        """★ 扫描在途时监听结束 ⇒ listen_btn 必须被重新启用、取消监听按钮要清掉。

        旧实现：监听收尾调 `_reset_scan_ui()`，而它在「还有扫描在途」时
        早退 ⇒ listen_btn 永久禁用、取消监听按钮残留在状态栏。
        """
        win.selected = _scanner("A", "192.168.1.1")
        win._start_scan()                              # 有扫描在途
        # 造一个「监听中」的状态
        win.listen_btn.setEnabled(False)
        win.cancel_listen_btn = gui.AnimatedButton("取消监听",
                                                   button_type="accent")
        win.status_bar.addPermanentWidget(win.cancel_listen_btn)

        win._reset_listen_ui()                         # 监听收尾走的路径
        assert win.listen_btn.isEnabled(), "listen_btn 没被重新启用"
        assert win.cancel_listen_btn is None, "取消监听按钮没被清掉"
