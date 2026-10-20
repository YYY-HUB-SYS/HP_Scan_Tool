r"""
统一命名形态护栏 — HP Scan Tool v4.0（2026-10-21 收口动作）

被守护的行为
------------
仓库里有**三个**入口会产出用户可见的产物名 / job_id：

    ① ``scan_coordinator.new_output_name`` / ``new_job_id``（直存 · 协调器）
    ② ``cli._timestamped_name``（命令行）
    ③ ``hp_scan_gui_pyside._timestamped_output_name`` / ``_timestamped_job_id``

★ 2026-10-21 之前它们是**两种形状**（两条并行修复线各选了一种）：

    ① ``HP_Scan_20261021_065432_e4404e9d.jpg``   （秒级时间戳 + 8 位 hex）
    ②③ ``HP_Scan_20261021_045213_123456_c3f19a.jpg`` （秒 + **微秒** + 1 字母 5 hex）

    ⇒ 用户在 GUI 里会看到**两种形状**：直存走 ①，会话落盘走 ③。

统一形态（主控拍板）
--------------------
    唯一成分  = **1 位字母（a–p）+ 7 位十六进制**（8 字符），**去掉微秒段**
    job_id    = ``{YYYYmmdd}_{HHMMSS}_{唯一成分}``
    输出文件名 = ``{前缀}_{YYYYmmdd}_{HHMMSS}_{唯一成分}.{扩展名}``

    例：``20261021_065432_a3f19a2c``
        ``HP_Scan_20261021_065432_a3f19a2c.jpg``

★ 为什么**去掉微秒段**：本机 Windows **系统时钟粒度实测 15.625 ms**
  ⇒ 同一 tick 内两次 ``strftime("%f")`` 的结果 **99.5% 完全相同**
  ⇒ 那 6 位微秒在同 tick 内**本来就一样、没有额外信息量**
  ⇒ 保留它是**虚假精度**（让人误以为精确到微秒）⇒ 删掉。
  真正的唯一性必须来自**随机成分**（uuid4 ⇒ 与时钟分辨率完全无关）。

★ 为什么唯一成分**首字符固定是字母**：产物名已有**分组编号**规则
  （``{base}_001.jpg`` / ``{base}_{组号}_{组内号}.ext``）
  ⇒ 纯数字的尾巴会被误读成组号（纯六进制随机约 6% 概率全数字）
  ⇒ 首字符固定字母 ⇒ 永远不会长得像组号。

★ 本文件还钉住「**只有一份实现**」：三个入口必须都走 ``naming`` 模块
  （否则形态迟早再次漂移 —— 形态漂移**正是**本轮要修的缺陷本身）。

★ 约束（本轮必须同时满足）
    - ``cli.py`` **不得** import Qt / GUI / 协调器（守护用例 ``tests/test_cli_no_qt.py``）
    - ``scan_coordinator.py`` **不得** import GUI
    - ⇒ 三个入口**不能互相 import** ⇒ 形态只能收敛到一个**零依赖**模块

GUI 一律 offscreen（``QT_QPA_PLATFORM=offscreen``）：不弹真实窗口、
不注入任何鼠标/键盘事件、不抢焦点；用户数据全部重定向到临时目录。
"""

import ast
import os
import re
import tempfile
from datetime import datetime as _real_datetime

import pytest

import cli
import naming
import scan_coordinator

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log
# 写日志。测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_naming_home_")
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


#: 统一形态：``{前缀}_{YYYYmmdd}_{HHMMSS}_{1 位字母 + 7 位十六进制}.{扩展名}``
OUTPUT_NAME_RE = re.compile(
    r"^(?P<prefix>[A-Za-z][A-Za-z0-9_]*)"
    r"_(?P<date>\d{8})"
    r"_(?P<clock>\d{6})"
    r"_(?P<token>[a-p][0-9a-f]{7})"
    r"\.(?P<ext>[A-Za-z0-9]+)$")

#: 统一形态：``{YYYYmmdd}_{HHMMSS}_{1 位字母 + 7 位十六进制}``
JOB_ID_RE = re.compile(
    r"^(?P<date>\d{8})_(?P<clock>\d{6})_(?P<token>[a-p][0-9a-f]{7})$")

#: 基名的**尾部三段**（前缀里可能也有下划线，如 ``HP_CLI`` ⇒ 从尾部对齐）
_STEM_TAIL_RE = re.compile(r"(?P<date>\d{8})_(?P<clock>\d{6})_"
                           r"(?P<token>[a-p][0-9a-f]{7})$")

#: 一个「同一 tick」内的固定时刻（两次取名拿到**同一秒**）
_FROZEN = _real_datetime(2026, 10, 21, 6, 54, 32, 123456)


class _FrozenClock:
    """永远返回**同一个**时刻的 datetime 替身（模拟「同一 tick」）。"""

    def __init__(self, moment):
        self._moment = moment

    def now(self, tz=None):
        return self._moment


def _freeze(monkeypatch, module):
    """把 ``module.datetime`` 换成冻结时钟 ⇒ 两次取名落在**同一秒**。"""
    monkeypatch.setattr(module, "datetime", _FrozenClock(_FROZEN))


@pytest.fixture
def frozen_clock(monkeypatch):
    """把三个入口的模块级 ``datetime`` 全部冻住 ⇒ 取名落在同一秒。"""
    for module in (cli, gui, scan_coordinator):
        _freeze(monkeypatch, module)


# ── 三个入口的取名函数（形态必须逐字一致）──────────────────────────────

def _cli_name(ext="jpg"):
    return cli._timestamped_name("HP_CLI", ext)


def _gui_name(ext="jpg"):
    return gui._timestamped_output_name("HP_Scan", ext)


def _coordinator_name(ext="jpg"):
    return scan_coordinator.new_output_name(ext)


_OUTPUT_PRODUCERS = ("cli", "gui", "coordinator")


def _produce(entry, ext="jpg"):
    if entry == "cli":
        return _cli_name(ext)
    if entry == "gui":
        return _gui_name(ext)
    return _coordinator_name(ext)


def _module_of(entry):
    return {"cli": cli, "gui": gui, "coordinator": scan_coordinator}[entry]


# ── 1. 三个入口的产物名形状一致 ────────────────────────────────────────

class TestUnifiedOutputNameShape:
    """★ 核心断言：三个入口产出**同一种形状**的产物名。"""

    @pytest.mark.parametrize("entry", _OUTPUT_PRODUCERS)
    def test_output_name_matches_the_unified_shape(self, entry, frozen_clock):
        name = _produce(entry)
        m = OUTPUT_NAME_RE.match(name)
        assert m, (
            "%s 入口的产物名不符合统一形态 %r：%r"
            % (entry, OUTPUT_NAME_RE.pattern, name))
        assert m.group("date") == "20261021"
        assert m.group("clock") == "065432"
        assert m.group("ext") == "jpg", "扩展名一字未改"

    @pytest.mark.parametrize("entry", _OUTPUT_PRODUCERS)
    def test_unique_component_starts_with_a_letter(self, entry):
        """★ 唯一成分首字符**必须是字母** ⇒ 永远不会被误读成组号/页号。"""
        name = _produce(entry)
        token = name.rsplit("_", 1)[-1].split(".", 1)[0]
        assert not token.isdigit(), (
            "唯一成分是纯数字 ⇒ 会被误读成组号/页号：%r" % name)
        assert token[0] in "abcdefghijklmnop", (
            "唯一成分首字符必须是 a–p 的字母：%r" % name)

    @pytest.mark.parametrize("entry", _OUTPUT_PRODUCERS)
    def test_output_name_has_no_microsecond_segment(self, entry):
        """★ 去掉微秒段：基名**尾部三段**是（日期 / 时分秒 / 唯一成分）。

        ★ 意图：微秒段是**虚假精度**（本机时钟粒度 15.625 ms ⇒ 同 tick 内
          99.5% 相同 ⇒ 没有额外信息量）⇒ 留着会让人误以为精确到微秒。
        """
        name = _produce(entry)
        stem = name.rsplit(".", 1)[0]
        m = _STEM_TAIL_RE.search(stem)
        assert m, (
            "基名尾部必须是「日期_时分秒_唯一成分」三段（不许再有微秒段）：%r"
            % stem)
        prefix = stem[:m.start()]
        assert not re.search(r"_\d{6}$", prefix), (
            "前缀末尾混进了 6 位数字段（伪微秒段）：%r" % stem)

    @pytest.mark.parametrize("entry", _OUTPUT_PRODUCERS)
    def test_two_names_in_the_same_tick_differ(self, entry, monkeypatch):
        """★ 同一 tick（同一秒）内两次取名必须不同。"""
        _freeze(monkeypatch, _module_of(entry))
        a = _produce(entry)
        b = _produce(entry)
        assert a != b, (
            "%s 入口同一秒内两次取名相同 ⇒ 会互相覆盖：%r / %r"
            % (entry, a, b))

    @pytest.mark.parametrize("entry", _OUTPUT_PRODUCERS)
    def test_many_names_in_the_same_tick_are_all_distinct(self, entry,
                                                           monkeypatch):
        """同一秒内连取 200 个名 ⇒ 全部不同（唯一成分不是摆设）。"""
        _freeze(monkeypatch, _module_of(entry))
        names = [_produce(entry) for _ in range(200)]
        assert len(set(names)) == 200, (
            "%s 入口同一秒内出现重复名字：%r"
            % (entry, [n for n in names if names.count(n) > 1][:5]))

    @pytest.mark.parametrize("prefix", ["HP_Scan", "HP_CLI", "HP_Listen",
                                        "HP_WIA"])
    def test_prefix_and_extension_contract_unchanged(self, prefix):
        """前缀 / 扩展名一字未改（只允许在时间戳之后追加唯一成分）。"""
        for ext in ("jpg", "png", "pdf", "tiff"):
            name = cli._timestamped_name(prefix, ext)
            assert name.startswith(prefix + "_") and name.endswith("." + ext), name
            assert OUTPUT_NAME_RE.match(name), name

    def test_all_three_entry_points_produce_the_same_shape(self, frozen_clock):
        """★ 三个入口的基名**逐段同形**（前缀与随机段归一后完全一致）。"""
        shapes = set()
        for entry in _OUTPUT_PRODUCERS:
            name = _produce(entry)
            m = OUTPUT_NAME_RE.match(name)
            assert m, name
            shapes.add("PREFIX_%s_%s_TOKEN.%s"
                       % (m.group("date"), m.group("clock"), m.group("ext")))
        assert len(shapes) == 1, (
            "三个入口的产物名形状不一致：%r" % (sorted(shapes),))


# ── 2. job_id 形状一致 ────────────────────────────────────────────────

class TestUnifiedJobIdShape:
    """★ 协调器与 GUI 的 job_id 必须是**同一种形状**。"""

    def test_coordinator_job_id_matches_the_unified_shape(self, frozen_clock):
        job_id = scan_coordinator.new_job_id()
        m = JOB_ID_RE.match(job_id)
        assert m, (
            "协调器 job_id 不符合统一形态 %r：%r"
            % (JOB_ID_RE.pattern, job_id))
        assert m.group("date") == "20261021"
        assert m.group("clock") == "065432"

    def test_gui_job_id_matches_the_unified_shape(self, frozen_clock):
        job_id = gui._timestamped_job_id()
        m = JOB_ID_RE.match(job_id)
        assert m, (
            "GUI job_id 不符合统一形态 %r：%r"
            % (JOB_ID_RE.pattern, job_id))
        assert m.group("date") == "20261021"
        assert m.group("clock") == "065432"

    def test_both_job_ids_have_the_same_shape(self, frozen_clock):
        """两个 job_id 的**形状**逐字一致（只有随机段不同）。"""
        a = scan_coordinator.new_job_id()
        b = gui._timestamped_job_id()
        assert re.sub(r"[a-p][0-9a-f]{7}$", "TOKEN", a) == \
            re.sub(r"[a-p][0-9a-f]{7}$", "TOKEN", b), (
            "协调器与 GUI 的 job_id 形状不一致：%r / %r" % (a, b))

    def test_job_id_is_safe_as_a_directory_name(self):
        """job_id 直接当**缓存目录名**用 ⇒ 绝不能含路径分隔符 / 非法字符。"""
        for job_id in (scan_coordinator.new_job_id(),
                       gui._timestamped_job_id()):
            assert ".." not in job_id
            assert os.sep not in job_id and "/" not in job_id
            for ch in '/\\:*?"<>|':
                assert ch not in job_id, (ch, job_id)
            assert re.fullmatch(r"[0-9a-z_]+", job_id), (
                "job_id 只允许 [0-9a-z_]（可安全作目录名 / page_meta key）：%r"
                % job_id)

    def test_two_job_ids_in_the_same_tick_differ(self, monkeypatch):
        _freeze(monkeypatch, scan_coordinator)
        _freeze(monkeypatch, gui)
        assert scan_coordinator.new_job_id() != gui._timestamped_job_id(), (
            "同一 tick 的两个 job_id 相同 ⇒ 共用缓存目录 ⇒ 页互相覆盖")


# ── 3. 只有一份实现（防形态再次漂移）──────────────────────────────────

class TestSingleImplementation:
    """★ 三个入口必须都走 ``naming`` ⇒ 形态不会再次分叉。"""

    def test_three_output_name_entry_points_delegate_to_naming(self,
                                                                monkeypatch):
        seen = []

        def _fake(prefix, ext, now=None):
            seen.append((prefix, ext))
            return "SENTINEL"

        monkeypatch.setattr(naming, "build_output_name", _fake)
        assert cli._timestamped_name("HP_CLI", "jpg") == "SENTINEL"
        assert gui._timestamped_output_name("HP_Scan", "jpg") == "SENTINEL"
        assert scan_coordinator.new_output_name("jpg") == "SENTINEL"
        assert [p for p, _ in seen] == ["HP_CLI", "HP_Scan", "HP_Scan"], (
            "三个入口没有全部走 naming.build_output_name：%r" % (seen,))

    def test_two_job_id_entry_points_delegate_to_naming(self, monkeypatch):
        seen = []

        def _fake(now=None):
            seen.append(now)
            return "SENTINEL"

        monkeypatch.setattr(naming, "build_job_id", _fake)
        assert scan_coordinator.new_job_id() == "SENTINEL"
        assert gui._timestamped_job_id() == "SENTINEL"
        assert len(seen) == 2, (
            "两个 job_id 入口没有全部走 naming.build_job_id：%r" % (seen,))

    def test_naming_module_has_zero_third_party_dependency(self):
        """★ ``naming`` 只准 import 标准库（uuid / datetime）⇒ 不拖 Qt。"""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "naming.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        assert imported <= {"uuid", "datetime"}, (
            "naming.py 只许依赖标准库 uuid / datetime，实际：%r"
            % (sorted(imported),))

    def test_cli_does_not_import_naming_via_qt(self):
        """★ ``cli`` 走 ``naming`` 时**不得**把 Qt 拖进来。"""
        source = open(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "cli.py"), encoding="utf-8").read()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        assert "naming" in imported, "cli.py 没有 import naming"
        for forbidden in ("PySide6", "tkinter", "scan_coordinator",
                          "hp_scan_gui_pyside"):
            assert forbidden not in imported, (
                "cli.py 的依赖链长出了 %r" % forbidden)
