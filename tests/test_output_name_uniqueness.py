r"""
输出文件名「同一时钟 tick 内撞名」回归测试 — HP Scan Tool
===========================================================

被守护的行为
------------
本机 Windows 的**系统时钟粒度 = 15.625 ms**
（`time.get_clock_info('time').resolution` 实测 0.015625）
⇒ 相邻两次 `datetime.now().strftime('%f')` 的结果 **99.5% 完全相同**
⇒ 只把产物名从「秒」提到「微秒」**并不能**避免撞名：

    同一 tick 内完成的两批扫描 ⇒ 算出**同一个文件名**
    ⇒ 后写的把先写的整个覆盖掉
    ⇒ 用户侧表现「刚才那批不见了」，**无任何报错**。

契约（本次修复后）
------------------
产物名 = `{前缀}_{YYYYmmdd_HHMMSS}_{8 位随机唯一成分}.{扩展名}`

* 唯一成分 = **1 位字母（a–p）+ 7 位十六进制**，来自 `uuid4()`
  ⇒ **不依赖时钟分辨率**。
* 时间戳仍在**前面** ⇒ 人一眼能看出大致何时产生（可读性不丢）。
* 扩展名 / 单页多页 / `save_manager.build_output_name` 的**分组编号规则**一字未改。
* **不用**「撞名加 -1/-2 序号」：产物名已有分组编号规则
  （`{base}_{组号}_{组内号}.ext` / `{base}_{N:03d}.ext`）⇒ 序号会与组号混淆。
* ★ 2026-10-21 统一形态：**去掉微秒段**（本机时钟粒度 15.625 ms ⇒
  同 tick 内 99.5% 相同 ⇒ 留着是**虚假精度**），并与协调器 / CLI 共用
  `naming` 模块的**同一份**实现（形态不再分叉）。

覆盖面
------
* CLI：`cli._timestamped_name` —— CLI 侧**没有** GUI 那层 `_OutputNameGuard` 兜底
  ⇒ 这个唯一性是**必需**的，不是冗余。
* GUI：`hp_scan_gui_pyside` 的 4 个用户可见输出名
  （会话默认 `HP_Scan_*` / 监听 `HP_Listen_*` ×2 / WIA 降级 `HP_WIA_*`）
  与 3 个 **job_id**（job_id 直接当 `cache_root/<job_id>/` 用
  ⇒ 同 id ⇒ 共用缓存目录 ⇒ 后写的页覆盖先写的页）。
* GUI 的**直存 `HP_Scan_*`** 名字由 `scan_coordinator.py` 生成（不在本次范围内）
  ⇒ 本文件只断言 GUI 侧不再**自己**造裸时间戳名。

GUI 一律 offscreen（`QT_QPA_PLATFORM=offscreen`）：不弹真实窗口、
不注入任何鼠标/键盘事件、不抢焦点；用户数据全部重定向到临时目录。
"""

import ast
import os
import pathlib
import re
import tempfile
from datetime import datetime as _real_datetime

import pytest

import cli

# 必须在创建 QApplication 之前设置：禁止弹出真实窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log
# 写日志。测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
_TMP_HOME = tempfile.mkdtemp(prefix="hp_outname_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
    import cache_manager
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI_PATH = os.path.join(PROJECT_ROOT, "hp_scan_gui_pyside.py")

# 一个「同一 tick」内的固定时刻（两次调用拿到的 `%f` 完全相同）
_FROZEN = _real_datetime(2026, 10, 21, 4, 52, 13, 123456)

# 产物名契约：{前缀}_{YYYYmmdd}_{HHMMSS}_{8位随机}.{ext}
# ★ 唯一成分 = 1 位字母（a–p）+ 7 位十六进制 ⇒ 尾巴**永远不会**长得像组号。
# ★ 2026-10-21：**去掉微秒段**（虚假精度）⇒ 基名只有三段。
_NAME_RE = re.compile(r"^(?P<prefix>[A-Za-z_]+)_(?P<date>\d{8})_(?P<clock>\d{6})"
                       r"_(?P<token>[a-p][0-9a-f]{7})\.(?P<ext>[A-Za-z0-9]+)$")


class _FrozenClock:
    """永远返回**同一个**时刻的 datetime 替身（模拟「同一 tick」）。"""

    def __init__(self, moment):
        self._moment = moment

    def now(self, tz=None):
        return self._moment


def _freeze_clock(monkeypatch, module):
    """把 `module.datetime` 换成冻结时钟 ⇒ 两次取名拿到**同一 tick**。"""
    monkeypatch.setattr(module, "datetime", _FrozenClock(_FROZEN))


def _has_no_microsecond_segment(name):
    """★ 产物名的基名里**不许**再有微秒段（6 位小数秒）。

    理由：本机系统时钟粒度 15.625 ms ⇒ 同一 tick 内两次 `%f` 99.5% 相同
    ⇒ 那 6 位**没有额外信息量** ⇒ 留着是**虚假精度**。

    判据：基名的**尾部三段**必须是「日期(8 位数字) / 时分秒(6 位数字) /
    唯一成分(1 字母 + 7 位十六进制)」⇒ 中间**没有**多出来的 6 位微秒段。
    （前缀里可能也有下划线，如 ``HP_CLI`` ⇒ 从**尾部**对齐，不按前缀切。）
    """
    stem = name.rsplit(".", 1)[0]
    return bool(re.search(r"\d{8}_\d{6}_[a-p][0-9a-f]{7}$", stem))


def _timestamp_name_call_sites(path):
    """返回文件里所有 `strftime(...)` 调用点所在的函数名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "strftime"):
            continue
        cur = node
        name = "<module>"
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = cur.name
                break
        sites.append(name)
    return sites


# ── CLI ──────────────────────────────────────────────────────────────────

class TestCliOutputName:
    """CLI 侧产物名（`cli._timestamped_name`）。"""

    def test_two_names_in_the_same_clock_tick_differ(self, monkeypatch):
        """★ 核心断言：**同一 tick** 内的两次取名必须不同。

        冻结时钟 ⇒ 两次拿到的 `%f` 完全相同 ⇒ 任何"只靠时间戳"的实现
        都会在这里撞名（不是空转断言）。
        """
        _freeze_clock(monkeypatch, cli)
        a = cli._timestamped_name("HP_CLI", "jpg")
        b = cli._timestamped_name("HP_CLI", "jpg")
        assert a != b, f"同一 tick 内两批产物同名 ⇒ 会互相覆盖: {a} / {b}"

    def test_name_keeps_timestamp_prefix_and_unique_suffix(self, monkeypatch):
        """★ 命名契约：时间戳在前（可读），唯一成分在后。"""
        _freeze_clock(monkeypatch, cli)
        name = cli._timestamped_name("HP_CLI", "jpg")
        m = _NAME_RE.match(name)
        assert m, f"产物名不符合契约: {name!r}"
        assert m.group("prefix") == "HP_CLI"
        assert m.group("date") == "20261021"
        assert m.group("clock") == "045213"
        assert m.group("ext") == "jpg", "扩展名必须一字未改"
        assert _has_no_microsecond_segment(name), (
            "产物名里不许再有微秒段（虚假精度）：%r" % name)

    def test_many_names_in_the_same_tick_are_all_distinct(self, monkeypatch):
        """同一 tick 内连续取 200 个名 ⇒ 全部不同（唯一成分不是摆设）。"""
        _freeze_clock(monkeypatch, cli)
        names = [cli._timestamped_name("HP_CLI", "jpg") for _ in range(200)]
        assert len(set(names)) == 200, (
            "同一 tick 内的名字出现重复：%r"
            % [n for n in names if names.count(n) > 1][:5])

    def test_unique_suffix_does_not_come_from_the_clock(self, monkeypatch):
        """★ 唯一成分必须**不依赖时钟** ⇒ 冻结时钟下它照样每次都变。"""
        _freeze_clock(monkeypatch, cli)
        a = cli._timestamped_name("HP_CLI", "jpg")
        b = cli._timestamped_name("HP_CLI", "jpg")
        token_a = a.rsplit("_", 1)[-1].split(".", 1)[0]
        token_b = b.rsplit("_", 1)[-1].split(".", 1)[0]
        assert token_a != token_b, (
            "唯一成分跟着时钟走 ⇒ 同一 tick 内必然撞名: %r / %r" % (a, b))
        assert token_a != "123456", (
            "唯一成分就是微秒本身 ⇒ 同一 tick 内必然撞名: %r" % a)
        assert _has_no_microsecond_segment(a), (
            "产物名里还留着微秒段（虚假精度）: %r" % a)

    def test_two_batches_in_same_tick_both_reach_disk(self, tmp_path, monkeypatch):
        """★ 用户侧症状的直接复现：同一 tick 的两批 ⇒ 磁盘上必须有两个产物。

        （真实场景：后写的那批把先写的那批整个覆盖掉，用户看不到任何报错。）
        """
        _freeze_clock(monkeypatch, cli)
        a = tmp_path / cli._timestamped_name("HP_CLI", "jpg")
        b = tmp_path / cli._timestamped_name("HP_CLI", "jpg")
        a.write_bytes(b"first-batch")
        b.write_bytes(b"second-batch")
        files = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
        assert len(files) == 2, f"两批产物只剩 {files}（后者覆盖了前者）"
        assert a.read_bytes() == b"first-batch"
        assert b.read_bytes() == b"second-batch"

    def test_prefix_and_extension_contract_unchanged(self, monkeypatch):
        """前缀 / 扩展名一字未改（只允许在时间戳之后追加唯一成分）。"""
        _freeze_clock(monkeypatch, cli)
        for ext in ("jpg", "png", "pdf", "tiff"):
            name = cli._timestamped_name("HP_CLI", ext)
            assert name.startswith("HP_CLI_") and name.endswith("." + ext), name


# ── GUI ──────────────────────────────────────────────────────────────────

class TestGuiOutputName:
    """GUI 侧用户可见的输出文件名。"""

    @pytest.mark.parametrize("prefix", ["HP_Scan", "HP_Listen", "HP_WIA"])
    def test_two_names_in_the_same_clock_tick_differ(self, monkeypatch, prefix):
        _freeze_clock(monkeypatch, gui)
        a = gui._timestamped_output_name(prefix, "pdf")
        b = gui._timestamped_output_name(prefix, "pdf")
        assert a != b, f"{prefix} 同一 tick 内两次取名相同 ⇒ 会互相覆盖: {a} / {b}"

    @pytest.mark.parametrize("prefix", ["HP_Scan", "HP_Listen", "HP_WIA"])
    def test_name_keeps_timestamp_prefix_and_unique_suffix(self, monkeypatch, prefix):
        _freeze_clock(monkeypatch, gui)
        name = gui._timestamped_output_name(prefix, "pdf")
        m = _NAME_RE.match(name)
        assert m, f"产物名不符合契约: {name!r}"
        assert m.group("prefix") == prefix
        assert m.group("date") == "20261021"
        assert m.group("clock") == "045213"
        assert m.group("ext") == "pdf"
        assert _has_no_microsecond_segment(name), (
            "产物名里不许再有微秒段（虚假精度）：%r" % name)

    def test_many_names_in_the_same_tick_are_all_distinct(self, monkeypatch):
        _freeze_clock(monkeypatch, gui)
        names = [gui._timestamped_output_name("HP_Scan", "pdf") for _ in range(200)]
        assert len(set(names)) == 200, "同一 tick 内出现重复名字"

    def test_gui_no_longer_builds_bare_timestamp_names(self):
        """★ 护栏：GUI 文件里**不许**再自己拼时间戳造名。

        ★ 2026-10-21 统一形态后，造名**全部委托**给 ``naming`` 模块
          （零依赖、单一实现）⇒ GUI 文件里**一个 `strftime` 调用都不该有**。
        """
        sites = _timestamp_name_call_sites(_gui_path())
        assert sites == [], (
            "GUI 里出现了裸时间戳造名（会撞名 / 形态会漂移）：%r" % (sites,))

    def test_listen_and_wia_sites_use_the_helper(self):
        """监听 ×2 / WIA ×1 / 会话默认 ×1 必须都走同一个造名入口。"""
        src = _gui_source()
        assert src.count("_timestamped_output_name(") >= 5, (
            "GUI 里的输出名造名入口数量异常（应 ≥5：helper 自身 + 4 个调用点）")
        assert 'HP_Listen' in src and 'HP_WIA' in src and 'HP_Scan' in src


class TestGuiJobId:
    """GUI 侧的 job_id（**同时就是缓存目录名**）。"""

    def test_two_job_ids_in_the_same_clock_tick_differ(self, monkeypatch):
        _freeze_clock(monkeypatch, gui)
        a = gui._timestamped_job_id()
        b = gui._timestamped_job_id()
        assert a != b, f"同一 tick 内两个 job_id 相同 ⇒ 共用缓存目录: {a} / {b}"

    def test_job_id_keeps_timestamp_and_is_a_safe_cache_dir_name(self, monkeypatch):
        _freeze_clock(monkeypatch, gui)
        job_id = gui._timestamped_job_id()
        m = re.match(r"^(\d{8})_(\d{6})_([a-p][0-9a-f]{7})$", job_id)
        assert m, f"job_id 不符合契约: {job_id!r}"
        assert m.group(1) == "20261021" and m.group(2) == "045213"
        assert not m.group(3).isdigit(), (
            "唯一成分是纯数字 ⇒ 会被误读成组号/页号: %r" % job_id)
        # 缓存目录名不能含路径分隔符 / ".."（cache_manager 有越界防护）
        assert ".." not in job_id
        assert os.sep not in job_id and "/" not in job_id

    def test_job_id_resolves_inside_cache_root(self, monkeypatch, tmp_path):
        """★ job_id 必须能解析成缓存根**之内**的目录（不被越界防护拒绝）。"""
        _freeze_clock(monkeypatch, gui)
        root = str(tmp_path / "cache_root")
        os.makedirs(root, exist_ok=True)
        monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: root)
        monkeypatch.setattr(cache_manager, "_config_cache", None)
        monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
        job_id = gui._timestamped_job_id()
        resolved = cache_manager._job_dir_path(job_id)
        assert os.path.abspath(resolved).startswith(os.path.abspath(root) + os.sep)

    def test_two_job_ids_do_not_share_cache_dir(self, monkeypatch, tmp_path):
        """★ 同 tick 的两个 job ⇒ 两个**不同**的缓存目录（页不会互相覆盖）。"""
        _freeze_clock(monkeypatch, gui)
        root = str(tmp_path / "cache_root")
        os.makedirs(root, exist_ok=True)
        monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: root)
        monkeypatch.setattr(cache_manager, "_config_cache", None)
        monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
        a = cache_manager._job_dir_path(gui._timestamped_job_id())
        b = cache_manager._job_dir_path(gui._timestamped_job_id())
        assert a != b, f"两个 job 共用缓存目录: {a}"


# ── 命名契约（分组编号规则）不许被改 ────────────────────────────────────

class TestGroupNumberingContractUntouched:
    """★ 本次修复只允许在**时间戳之后**追加唯一成分 ⇒ 分组编号规则一字未改。"""

    def test_build_output_name_shapes(self):
        import save_manager
        assert save_manager.build_output_name("C:/out/HP_Scan", "jpg", 1, 1, 1) \
            == "C:/out/HP_Scan_001.jpg"
        assert save_manager.build_output_name("C:/out/HP_Scan", "pdf", 2, 1, 1) \
            == "C:/out/HP_Scan_001.pdf"
        assert save_manager.build_output_name("C:/out/HP_Scan", "jpg", 2, 2, 3) \
            == "C:/out/HP_Scan_002_003.jpg"

    def test_new_unique_suffix_does_not_collide_with_group_numbers(self):
        """★ 唯一成分不能长得像组号（`_001`）⇒ 不会与分组编号混淆。

        契约：**唯一成分永远至少含一个字母**（纯六进制有 ~6% 概率全数字
        ⇒ 那样会被误读成"又一个数字段"）。
        """
        import save_manager
        for _ in range(50):
            name = gui._timestamped_output_name("HP_Scan", "jpg")
            base = name[:-len(".jpg")]
            token = base.rsplit("_", 1)[-1]
            assert not token.isdigit(), (
                "唯一成分是纯数字 ⇒ 会被误读成组号/页号: %r" % name)
        assert save_manager.build_output_name(base, "jpg", 2, 1, 1) \
            == base + "_001_001.jpg"


# ── 第二层兜底：_OutputNameGuard 仍然有效 ───────────────────────────────

class TestOutputNameGuardStillWorks:
    """GUI 侧的第二层兜底（改用 `{base}_1`、`{base}_2`…）仍然成立。"""

    def test_guard_dedups_the_same_path(self, tmp_path):
        same = str(tmp_path / "HP_Scan_20261021_034200.pdf")
        first = gui._unique_output_path(same)
        second = gui._unique_output_path(same)
        assert first != second, "兜底守卫失效：两次占用拿到同一个路径"

    def test_guard_is_the_second_layer_not_the_only_one(self, monkeypatch):
        """★ 兜底是**第二层**：第一层（唯一成分）已保证名字本身就不撞。"""
        _freeze_clock(monkeypatch, gui)
        a = gui._timestamped_output_name("HP_Scan", "pdf")
        b = gui._timestamped_output_name("HP_Scan", "pdf")
        assert a != b, "第一层（唯一成分）没起作用"


def _gui_path():
    return pathlib.Path(GUI_PATH)


def _gui_source():
    with open(GUI_PATH, "r", encoding="utf-8") as fh:
        return fh.read()
