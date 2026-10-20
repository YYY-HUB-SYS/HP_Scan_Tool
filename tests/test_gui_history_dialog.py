"""
GUI 扫描历史对话框**显示**测试 — HP Scan Tool v4.0
覆盖链号 16.16：「无用例构造过该对话框」。

被守护的行为（源码事实，hp_scan_gui_pyside.py:5422-5472）
---------------------------------------------------------
    class HistoryDialog(QDialog):
        def __init__(self, parent):
            ...
            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["时间", "设备", "页数", "格式", "路径"])
            self._load_history()
        def _load_history(self):
            history = history_manager.list_records(limit=9999)
            for entry in reversed(history):
                item = QTreeWidgetItem([
                    entry.get("timestamp", "")[:19],
                    entry.get("device_name", ""),
                    str(entry.get("page_count", 1)),
                    entry.get("format", ""),
                    entry.get("file_path", ""),
                ])
                self.tree.addTopLevelItem(item)

⇒ 对话框必须**真的把历史显示出来**：条目数对得上、每行的文件名对得上。
  `list_records` 返回的是**最新在前**，界面按 `reversed()` 排成**最旧在上**。

★ GUI 一律 offscreen：不 show()、不抢焦点、不弹真实窗口。
★ 历史数据是**假的**：写进 conftest 的 APPDATA 沙箱，
  绝不读真实用户的 `%APPDATA%\\HP_Scan_Tool\\history.json`。
"""

import os
import tempfile

import pytest

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
# 测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_gui_history_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)

import history_manager


# ── 夹具 ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def qapp(qt_app):
    """复用 conftest 的会话级 QApplication（offscreen，不显示窗口）。"""
    return qt_app


@pytest.fixture
def make_dialog(qapp):
    """构造一个（**不显示**的）历史对话框，用完关掉。

    ★ 是工厂而不是实例：HistoryDialog 在**构造时**就把历史读进树里
      ⇒ 必须先造好假历史、再构造对话框，顺序反了就只能看到 0 行。
    """
    created = []

    def _make():
        dlg = gui.HistoryDialog(None)
        created.append(dlg)
        return dlg

    try:
        yield _make
    finally:
        for dlg in created:
            dlg.close()


def _record(idx):
    """第 idx 条假记录（idx 从 1 开始，越大越新）。"""
    return {
        "timestamp": "2026-10-21T0%d:00:00" % idx,
        "device_name": "HP M233",
        "device_ip": "192.168.7.%d" % idx,
        "page_count": idx,
        "format": "pdf",
        "file_path": os.path.join("C:\\out", "gui_seed_%d.pdf" % idx),
        "source": "test",
    }


def _seed(count):
    """把 count 条假历史写进沙箱，返回记录列表（写入顺序 = 最旧→最新）。"""
    records = [_record(i + 1) for i in range(count)]
    for rec in records:
        assert history_manager.append(rec) is True, "历史写入失败（沙箱不可写？）"
    return records


def _rows(dlg):
    """取出对话框里的全部行（每行 5 列的文本列表）。"""
    tree = dlg.tree
    return [
        [tree.topLevelItem(i).text(c) for c in range(tree.columnCount())]
        for i in range(tree.topLevelItemCount())
    ]


# ── 显示条数 ───────────────────────────────────────────────

class TestHistoryDialogEntryCount:
    """对话框显示的条目数必须与历史条数一致。"""

    def test_dialog_shows_all_seeded_entries(self, make_dialog):
        """3 条历史 ⇒ 对话框 3 行。"""
        _seed(3)

        dlg = make_dialog()
        assert dlg.tree.topLevelItemCount() == 3, (
            "应显示 3 条历史，实际 %d 条" % dlg.tree.topLevelItemCount())

    def test_dialog_shows_no_entries_when_history_is_empty(self, make_dialog):
        """没有历史 ⇒ 0 行，且构造过程不崩。"""
        dlg = make_dialog()
        assert dlg.tree.topLevelItemCount() == 0

    def test_dialog_shows_exactly_the_saved_record_count(self, make_dialog):
        """条目数 == 沙箱里真实落盘的记录数（不是固定值）。"""
        _seed(5)

        dlg = make_dialog()
        assert dlg.tree.topLevelItemCount() == 5
        assert len(history_manager.list_records(limit=9999)) == 5


# ── 显示内容 ───────────────────────────────────────────────

class TestHistoryDialogContent:
    """每行显示的内容（文件名 / 设备 / 页数 / 格式）必须正确。"""

    def test_rows_show_file_names(self, make_dialog):
        """★ 核心断言：每行的**路径列**是历史记录里的 file_path。"""
        seeded = _seed(3)

        dlg = make_dialog()
        rows = _rows(dlg)
        assert len(rows) == 3
        got_paths = [r[4] for r in rows]
        assert got_paths == [r["file_path"] for r in seeded], (
            "路径列不对：%r" % (got_paths,))
        for i in (1, 2, 3):
            assert any("gui_seed_%d.pdf" % i in p for p in got_paths), (
                "第 %d 条的文件名没显示出来：%r" % (i, got_paths))

    def test_rows_show_device_page_count_and_format(self, make_dialog):
        """设备 / 页数 / 格式三列也要对（页数是字符串化的整数）。"""
        _seed(2)

        dlg = make_dialog()
        rows = _rows(dlg)
        assert [r[1] for r in rows] == ["HP M233", "HP M233"]
        assert [r[2] for r in rows] == ["1", "2"]
        assert [r[3] for r in rows] == ["pdf", "pdf"]
        assert [r[0] for r in rows] == ["2026-10-21T01:00:00",
                                        "2026-10-21T02:00:00"]

    def test_rows_are_ordered_oldest_first(self, make_dialog):
        """界面顺序：最旧在上（list_records 是最新在前 ⇒ reversed）。"""
        _seed(3)

        dlg = make_dialog()
        stamps = [r[0] for r in _rows(dlg)]
        assert stamps == sorted(stamps), "行序应是最旧→最新，实际 %r" % (stamps,)
        assert stamps[0] == "2026-10-21T01:00:00"
        assert stamps[-1] == "2026-10-21T03:00:00"


# ── 用户数据零污染 ─────────────────────────────────────────

class TestHistoryDialogDataIsolation:
    """对话框读的是沙箱历史，**不是**真实用户历史。"""

    def test_history_file_resolves_inside_sandbox(self, tmp_path):
        """历史文件路径必须落在 tmp_path 沙箱里，且**不在**真实用户目录。"""
        _seed(1)

        path = os.path.abspath(history_manager._history_file())
        sandbox = os.path.abspath(str(tmp_path))
        assert path.startswith(sandbox), (
            "历史文件不在沙箱内：%s（沙箱 %s）" % (path, sandbox))

        real_dir = os.path.abspath(os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming", "HP_Scan_Tool"))
        assert not path.startswith(real_dir), (
            "正在读真实用户历史文件：%s" % path)

    def test_dialog_does_not_touch_real_user_history(self, make_dialog):
        """构造对话框不会读到真实用户的历史（沙箱里没有 ⇒ 0 行）。"""
        real_dir = os.path.abspath(os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming", "HP_Scan_Tool"))

        dlg = make_dialog()
        # 真实用户目录里有历史（基线 6 条），但对话框一条都不该显示
        assert dlg.tree.topLevelItemCount() == 0
        rows = _rows(dlg)
        assert all(not r[4].startswith(real_dir) for r in rows)
