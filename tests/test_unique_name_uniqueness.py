r"""
唯一命名护栏 — HP Scan Tool v4.0（批次 5 新增护栏）

★ 要修的缺陷（**实测复现**，不是推断）
--------------------------------------------------------------------------
Windows 系统时钟分辨率 = **15.625ms**（实测
``time.get_clock_info('time').resolution == 0.015625``）⇒ 相邻两次
``datetime.now().strftime('%f')`` 的结果 **99.5% 完全相同**
⇒ 任何"把时间戳当唯一键"的地方都会撞名。

本文件覆盖本仓库里**剩下的三处**同族缺陷：

1) ``scan_coordinator`` 的 **job_id** —— job_id **就是缓存目录名**
   （``cache_manager._job_dir(job_id)``，页文件写在里面）
   ⇒ 同一 tick 内起的两次扫描**共用一个缓存目录**
   ⇒ 后一次 ``write_page`` 用 ``open(path, "wb")`` **覆盖**前一次的页
   ⇒ **丢的是扫描产物**（用户侧表现："刚才那批页不见了"，**无任何报错**）。

2) ``scan_coordinator`` 的**输出文件名** —— ``HP_Scan_<ts>.<ext>``
   ⇒ 同一 tick 的两次扫描算出同一个文件名 ⇒ 后者**静默覆盖**前者的产物。

3) ``document_session`` 的**损坏备份名** —— ``.corrupt-<%Y%m%d-%H%M%S-%f>``
   ⇒ 与 ``page_meta`` / ``preset_manager`` / ``history_manager`` /
   ``profile_manager`` **完全同一机制**（那 4 个模块已用
   「``os.open(O_CREAT|O_EXCL)`` 独占创建占名 + 撞名加 ``-2``/``-3`` 序号」修好）
   ⇒ 本文件把同一套方案落到 ``document_session`` 上。

★ 本文件为什么能**稳定**复现（而不是 99.5% 的概率）
--------------------------------------------------------------------------
把被测模块的**模块级 ``datetime``** 换成"永远返回同一时刻"的替身
⇒ 两次算出的时间戳**必然相同** ⇒ 旧实现**稳定红**，新实现**稳定绿**
⇒ 不依赖概率 ⇒ 可以当长期护栏，而不是靠"多跑几次碰运气"。

★ 不能被破坏的既有契约（本文件同时当回归护栏）
--------------------------------------------------------------------------
- job_id 仍是**内部标识**（缓存目录名 / page_meta 的 key / 待保存表）：
  唯一即可，但**不得**含路径分隔符 / 非法字符
- 输出文件名的**命名规则一字未改**（扩展名 / 单页多页 / ``build_output_name``
  的分组编号）⇒ 只允许在**时间戳之后**追加唯一成分
- 首个损坏备份名仍是 ``<name>.corrupt-<YYYYMMDD-HHMMSS-ffffff>``
  （既有断言 ``^xxx\.corrupt-\d{8}-\d{6}-\d{6}$`` 依赖它）
"""

import os
import re
import time
from datetime import datetime as _real_datetime

import pytest
from unittest import mock

import config_io
import document_session
import save_manager
import scan_coordinator
from document_session import SessionStore
from scan_coordinator import ScanCoordinator


#: 冻结时刻 —— 两次调用必然算出同一个时间戳
_FROZEN = _real_datetime(2026, 10, 21, 6, 54, 32, 123456)
#: 冻结时刻的"秒级"时间戳（job_id / 输出名的可读性前缀）
_FROZEN_TS = _FROZEN.strftime("%Y%m%d_%H%M%S")
#: 冻结时刻的"微秒级"时间戳（损坏备份名沿用旧格式）
_FROZEN_TS_US = _FROZEN.strftime("%Y%m%d-%H%M%S-%f")


class _FrozenDatetime:
    """模块级 ``datetime`` 的替身：``now()`` 永远返回同一个时刻"""

    @staticmethod
    def now():
        return _FROZEN


class _FakeUuid:
    """``uuid.uuid4()`` 的替身：``.hex`` 是固定值（用于**确定性地**验证来源）"""

    def __init__(self, hex_value):
        self.hex = hex_value


def _freeze_clock(monkeypatch, module):
    """把被测模块的时钟**冻住** ⇒ 时间戳必然撞名（把概率问题变成必然问题）"""
    monkeypatch.setattr(module, "datetime", _FrozenDatetime, raising=True)


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def coordinator():
    c = ScanCoordinator()
    c.cfg = {}
    return c


@pytest.fixture
def scanner():
    from escl_engine import ScannerInfo
    s = ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    s.has_adf = False
    return s


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    root = str(tmp_path / "cache")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_default_cache_root",
                        lambda: root)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_cache_config_path",
                        lambda: os.path.join(root, "cache_config.json"))
    monkeypatch.setattr(scan_coordinator.cache_manager, "_config_cache", None)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_active_jobs", set())
    monkeypatch.setattr(scan_coordinator.cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(scan_coordinator.cache_manager, "_cache_size_cache", None)
    return root


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    """把 config_io.config_dir 指向 tmp_path —— 测试绝不碰真实 %APPDATA%"""
    base = tmp_path / "appdata" / "HP_Scan_Tool"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_io, "config_dir", lambda: str(base))
    return str(base)


@pytest.fixture
def store(sessions_root, monkeypatch):
    # 去重集合是模块级的 ⇒ 每个用例从空集合开始，避免用例间互相影响
    monkeypatch.setattr(document_session, "_CORRUPT_BACKED_UP", set())
    return SessionStore()


def _params(tmp_path, **kw):
    p = {
        "resolution": 300,
        "color_mode_ui": "彩色",
        "output_format": "jpg",
        "source": "平板",
        "duplex": False,
        "output_dir": str(tmp_path),
    }
    p.update(kw)
    return p


# ── 1. job_id 唯一性 ─────────────────────────────────────────────────


class TestJobIdUniqueness:
    """job_id = 缓存目录名 ⇒ 必须真正唯一，且**不依赖时钟分辨率**"""

    def test_two_job_ids_same_frozen_clock_differ(self, monkeypatch):
        """同一 tick 内生成的两个 job_id 必须不同（旧实现稳定红）。"""
        _freeze_clock(monkeypatch, scan_coordinator)
        a = scan_coordinator.new_job_id()
        b = scan_coordinator.new_job_id()
        assert a != b, (
            "同一时钟 tick 内生成的两个 job_id 必须不同（旧实现用时间戳当唯一键，"
            "时钟粒度 15.625ms ⇒ 99.5% 撞名 ⇒ 两次扫描共用一个缓存目录、"
            "互相覆盖页）：%r == %r" % (a, b))

    def test_unique_component_is_not_the_clock_value(self, monkeypatch):
        """唯一成分必须来自**随机源**，不是时钟值。

        ⇒ 冻结时钟时，前缀（时间戳）完全相同，但后缀必须不同。
        ⇒ 这就是"不依赖时钟分辨率"的直接证据。
        """
        _freeze_clock(monkeypatch, scan_coordinator)
        a = scan_coordinator.new_job_id()
        b = scan_coordinator.new_job_id()
        assert a[:len(_FROZEN_TS)] == _FROZEN_TS, (
            "job_id 必须保留可读的时间戳前缀 %r：%r" % (_FROZEN_TS, a))
        assert b[:len(_FROZEN_TS)] == _FROZEN_TS
        tail_a = a[len(_FROZEN_TS):]
        tail_b = b[len(_FROZEN_TS):]
        assert tail_a != tail_b, (
            "唯一成分必须与时钟值不同：%r vs %r" % (a, b))
        assert re.fullmatch(r"_[0-9a-f]{8}", tail_a), (
            "唯一成分应为 8 位十六进制（uuid4 前 8 位，仓库既有惯例）：%r" % a)

    def test_job_id_is_safe_as_directory_name(self, monkeypatch):
        """job_id 会被直接当**目录名**用 ⇒ 绝不能含路径分隔符 / 非法字符。"""
        _freeze_clock(monkeypatch, scan_coordinator)
        job_id = scan_coordinator.new_job_id()
        for ch in '/\\:*?"<>|':
            assert ch not in job_id, (
                "job_id 含非法字符 %r（会被当成目录名用）：%r" % (ch, job_id))
        assert ".." not in job_id
        assert not os.path.isabs(job_id)
        assert job_id == os.path.basename(job_id)

    def test_unique_component_comes_from_uuid4_not_the_clock(self, monkeypatch):
        """★ 确定性地证明「唯一成分来自 uuid4，而不是时钟」。

        把 ``uuid.uuid4`` 换成固定值 ⇒ 输出必须**逐字符**等于预期值。
        ⇒ 旧实现（唯一成分 = 时钟的微秒）在这里**必然红**，
        且本断言**零概率性**（不像"比较微秒值"那种 1/16^6 的偶发命中）。
        """
        _freeze_clock(monkeypatch, scan_coordinator)
        monkeypatch.setattr(scan_coordinator.uuid, "uuid4",
                            lambda: _FakeUuid("deadbeefcafe0000"))
        assert scan_coordinator.new_job_id() == "20261021_065432_deadbeef", (
            "job_id 的唯一成分必须来自 uuid4（时间戳只负责可读性）")

    def test_run_scan_sync_same_tick_two_scans_keep_both_pages(
            self, coordinator, scanner, tmp_path, cache_root, monkeypatch):
        """★ 端到端护栏：同一 tick 内连扫两次 ⇒ 两个 job、两个缓存目录、两批页都在。

        这是第 1 条缺陷的**真实后果**：旧实现下两次扫描共用一个缓存目录，
        后一次 ``write_page`` 直接 ``open(path, "wb")`` 覆盖前一次的页
        ⇒ **扫描产物丢失且零报错**。
        """
        _freeze_clock(monkeypatch, scan_coordinator)
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(b"\xff\xd8fake", "jpg")):
            r1 = coordinator.run_scan_sync(scanner, _params(tmp_path))
            r2 = coordinator.run_scan_sync(scanner, _params(tmp_path))

        assert r1.job_id != r2.job_id, (
            "同一 tick 的两次扫描拿到了同一个 job_id ⇒ 共用缓存目录：%r" % r1.job_id)
        assert os.path.dirname(r1.cached_paths[0]) != \
            os.path.dirname(r2.cached_paths[0]), (
            "两次扫描的缓存目录必须不同（job_id 就是缓存目录名）")
        assert os.path.exists(r1.cached_paths[0]), "第一批页不见了（被覆盖了）"
        assert os.path.exists(r2.cached_paths[0]), "第二批页不见了"

    def test_start_scan_same_tick_gives_two_different_job_ids(
            self, coordinator, scanner, tmp_path, cache_root, monkeypatch):
        """异步入口（``start_scan``）同样必须给出不同的 job_id。

        ★ mock 必须**覆盖整个扫描过程**（``start_scan`` 立即返回，扫描在
        线程池里跑）⇒ 在 ``with`` 块内 ``waitForDone``，否则 mock 撤掉后
        线程会走真实引擎 ⇒ 发出真实 HTTP（违反"不发真实请求"的铁律）。
        """
        _freeze_clock(monkeypatch, scan_coordinator)
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(b"\xff\xd8fake", "jpg")):
            jid1 = coordinator.start_scan(scanner, _params(tmp_path))
            jid2 = coordinator.start_scan(scanner, _params(tmp_path))
            coordinator._pool.waitForDone(5000)
        try:
            assert jid1 != jid2, (
                "start_scan 同一 tick 生成的两个 job_id 相同：%r" % jid1)
        finally:
            coordinator.shutdown()


# ── 2. 输出文件名唯一性 ───────────────────────────────────────────────


class TestOutputNameUniqueness:
    """输出文件名必须真正唯一 ⇒ 后一次扫描不能静默覆盖前一次的产物"""

    def test_two_output_names_same_frozen_clock_differ(self, monkeypatch):
        """同一 tick 内的两个输出文件名必须不同（旧实现稳定红）。"""
        _freeze_clock(monkeypatch, scan_coordinator)
        a = scan_coordinator.new_output_name("jpg")
        b = scan_coordinator.new_output_name("jpg")
        assert a != b, (
            "同一时钟 tick 内的两个输出文件名必须**不同**"
            "（旧实现用时间戳当唯一键 ⇒ 后者静默覆盖前者的产物）：%r == %r" % (a, b))

    def test_output_name_keeps_timestamp_prefix_and_extension(self, monkeypatch):
        """可读性与扩展名契约不能变：``HP_Scan_<时间戳>_<唯一成分>.<ext>``。"""
        _freeze_clock(monkeypatch, scan_coordinator)
        name = scan_coordinator.new_output_name("jpg")
        assert name.startswith("HP_Scan_" + _FROZEN_TS), (
            "输出名必须以 HP_Scan_<时间戳> 开头（时间戳在前）：%r" % name)
        assert name.endswith(".jpg"), "扩展名不能变：%r" % name
        base = name[:-len(".jpg")]
        m = re.fullmatch(r"HP_Scan_\d{8}_\d{6}_[0-9a-f]{6}", base)
        assert m, (
            "唯一成分应为 6 位十六进制（与组号/页号规则不冲突）：%r" % base)

    def test_output_name_unique_component_comes_from_uuid4_not_the_clock(
            self, monkeypatch):
        """★ 确定性地证明「输出名的唯一成分来自 uuid4，而不是时钟」。

        把 ``uuid.uuid4`` 换成固定值 ⇒ 输出名必须**逐字符**等于预期值。
        ⇒ 旧实现（唯一成分 = 时钟的微秒）在这里**必然红**，且零概率性。
        """
        _freeze_clock(monkeypatch, scan_coordinator)
        monkeypatch.setattr(scan_coordinator.uuid, "uuid4",
                            lambda: _FakeUuid("deadbeefcafe0000"))
        assert scan_coordinator.new_output_name("jpg") == \
            "HP_Scan_20261021_065432_deadbe.jpg", (
            "输出名的唯一成分必须来自 uuid4（时间戳只负责可读性）")

    def test_output_name_does_not_use_sequence_suffix(self, monkeypatch):
        """★ 不得用「撞名加 -1/-2 序号」——输出名已有**分组编号**规则。

        分组编号是 **3 位零填充**（``_001`` / ``_002``），唯一成分是 **6 位**，
        ⇒ 两者在形态上不可能混淆。
        """
        _freeze_clock(monkeypatch, scan_coordinator)
        base = scan_coordinator.new_output_name("jpg")[:-len(".jpg")]
        suffix = base.rsplit("_", 1)[-1]
        assert len(suffix) == 6, (
            "唯一成分长度固定为 6（组号是 3 位零填充 ⇒ 形态不混淆）：%r" % base)
        # 唯一成分**不能**是 3 位零填充的组号形态
        assert not re.fullmatch(r"\d{3}", suffix), (
            "唯一成分不得长得像组号（3 位零填充）：%r" % base)

    def test_build_output_name_group_numbering_unchanged(self, tmp_path):
        """★ 命名契约：协调器给出的输出名仍是**基名**，
        ``save_manager.build_output_name`` 的分组编号规则**一字未改**。"""
        base = os.path.join(str(tmp_path), "HP_Scan_20261021_065432_ab12cd")
        assert save_manager.build_output_name(base, "jpg", 1, 1, 1) == \
            base + "_001.jpg"
        assert save_manager.build_output_name(base, "jpg", 2, 1, 2) == \
            base + "_001_002.jpg"
        assert save_manager.build_output_name(base, "pdf", 1, 1, 1) == \
            base + ".pdf"
        assert save_manager.build_output_name(base, "pdf", 2, 2, 1) == \
            base + "_002.pdf"

    def test_run_scan_sync_same_tick_two_scans_give_two_output_paths(
            self, coordinator, scanner, tmp_path, cache_root, monkeypatch):
        """端到端：同一 tick 的两次扫描 ⇒ 两个不同的输出路径。"""
        _freeze_clock(monkeypatch, scan_coordinator)
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(b"\xff\xd8fake", "jpg")):
            r1 = coordinator.run_scan_sync(scanner, _params(tmp_path))
            r2 = coordinator.run_scan_sync(scanner, _params(tmp_path))
        assert r1.output_path != r2.output_path, (
            "同一 tick 的两次扫描算出了同一个输出文件名 ⇒ 后者覆盖前者：%r"
            % r1.output_path)
        assert os.path.basename(r1.output_path) != \
            os.path.basename(r2.output_path)


# ── 3. document_session 损坏备份名 ───────────────────────────────────


class TestDocumentSessionCorruptBackupUniqueness:
    """会话文件损坏备份：两次损坏 ⇒ 两个备份，互不覆盖

    ★ 与 ``page_meta`` / ``preset_manager`` / ``history_manager`` /
    ``profile_manager`` **同一套方案**：
    「``os.open(O_CREAT|O_EXCL)`` 独占创建占名 + 撞名加 ``-2``/``-3`` 序号」
    ⇒ 首个备份名与旧格式**逐字节同形**（既有正则断言仍成立）。
    """

    def _corrupt_twice(self, store, monkeypatch):
        _freeze_clock(monkeypatch, document_session)
        sid = "sess_broken"
        path = store.path_for(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        first = b'{"broken": [1, 2,'
        second = b'{"broken": [1, 2, 3, 4, 5]'
        with open(path, "wb") as f:
            f.write(first)
        store.load(sid)

        with open(path, "wb") as f:
            f.write(second)
        store.load(sid)
        return path, first, second

    def _backups(self, directory, base):
        return sorted(f for f in os.listdir(directory)
                      if f.startswith(base + ".corrupt-"))

    def test_two_corrupt_writes_same_frozen_clock_give_two_backups(
            self, store, monkeypatch):
        """★ 同一 tick 内两次损坏 ⇒ 两个备份（旧实现稳定红：只剩 1 个）。"""
        path, first, second = self._corrupt_twice(store, monkeypatch)
        directory = os.path.dirname(path)
        backups = self._backups(directory, os.path.basename(path))
        assert len(backups) == 2, (
            "两次损坏必须留下两个备份（时间戳相同时也不能互相覆盖），实际: %r"
            % (backups,))

        contents = sorted(open(os.path.join(directory, b), "rb").read()
                          for b in backups)
        assert contents == sorted([first, second]), (
            "两个备份必须各自保存当次的损坏原文：%r" % (contents,))

    def test_first_backup_name_keeps_legacy_shape(self, store, monkeypatch):
        """★ 首个备份名必须仍是旧格式（既有断言的正则形状不能破）。"""
        path, _first, _second = self._corrupt_twice(store, monkeypatch)
        directory = os.path.dirname(path)
        base = os.path.basename(path)
        backups = self._backups(directory, base)
        pattern = r"^%s\.corrupt-\d{8}-\d{6}-\d{6}$" % re.escape(base)
        assert any(re.match(pattern, b) for b in backups), (
            "首个备份名必须保持 <name>.corrupt-<YYYYMMDD-HHMMSS-ffffff> 形状：%r"
            % (backups,))

    def test_second_backup_gets_sequence_suffix(self, store, monkeypatch):
        """撞名的第二份备份追加 ``-2``（同前缀、同时间戳 ⇒ 仍看得出是同一批）。"""
        path, _first, _second = self._corrupt_twice(store, monkeypatch)
        directory = os.path.dirname(path)
        base = os.path.basename(path)
        backups = self._backups(directory, base)
        assert any(b.endswith("-2") for b in backups), (
            "第二个备份应带 -2 序号后缀：%r" % (backups,))
        for b in backups:
            assert b.startswith(base + ".corrupt-" + _FROZEN_TS_US), (
                "备份名必须保留同一时间戳前缀：%r" % (backups,))

    def test_corrupt_backup_keeps_original_file(self, store, monkeypatch):
        """备份**不删原文件**（既有语义不能变）。"""
        path, _first, _second = self._corrupt_twice(store, monkeypatch)
        assert os.path.exists(path), "损坏的会话原文件被删了"
        with open(path, "rb") as f:
            assert f.read() == b'{"broken": [1, 2, 3, 4, 5]'

    def test_backup_failure_leaves_no_placeholder(self, store, monkeypatch,
                                                  tmp_path):
        """★ 复制失败时不得留下**空的占位文件**（独占创建只占名，不写内容）。"""
        _freeze_clock(monkeypatch, document_session)
        sid = "sess_locked"
        path = store.path_for(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b'{"broken": [1, 2,')

        real_copyfile = document_session.shutil.copyfile

        def _boom(src, dst, *a, **kw):
            raise OSError("模拟复制失败")

        monkeypatch.setattr(document_session.shutil, "copyfile", _boom)
        try:
            result = document_session._backup_corrupt(path)
        finally:
            monkeypatch.setattr(document_session.shutil, "copyfile",
                                real_copyfile)
        assert result is None, "复制失败必须返回 None"
        leftovers = [f for f in os.listdir(os.path.dirname(path))
                     if f.startswith(os.path.basename(path) + ".corrupt-")]
        assert leftovers == [], (
            "复制失败后不得留下空的占位文件：%r" % (leftovers,))


# ── 4. 时钟粒度实测（护栏本身的事实基础）─────────────────────────────


def test_clock_resolution_is_coarse_enough_to_collide():
    """★ 本文件所有用例的事实基础：系统时钟粒度粗到"相邻两次取时刻会相同"。

    若将来换到高精度时钟，这条会红 —— 那**不是缺陷**，而是提醒：
    唯一性仍然必须成立（见上面各用例），但概率性复现不再成立。
    """
    resolution = time.get_clock_info("time").resolution
    assert resolution >= 0.001, (
        "系统时钟分辨率意外变高（%r）—— 本文件用冻结时钟复现，结论不变" % resolution)
