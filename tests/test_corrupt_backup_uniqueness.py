"""
损坏备份命名唯一性 — HP Scan Tool v4.0（批次 5 新增护栏）

★ 要修的缺陷（**实测复现**，不是推断）
----------------------------------------------------------------------
Windows 系统时钟分辨率 = **15.625ms**（实测
``time.get_clock_info('time').resolution == 0.015625``）⇒ 相邻两次
``datetime.now().strftime('%f')`` 的结果 **49/50 次完全相同** ⇒ 两次损坏
备份算出**同一个文件名** ⇒ 第二次把第一次**覆盖** ⇒ 只剩 1 个备份 ⇒
**证据丢了**。

缺陷方实测（脱离 pytest 连跑 40 次）：
``tests/test_profile_manager.py::TestCorruptProtection::
test_two_corrupt_writes_produce_two_different_backups``
⇒ **失败 3 次**（只剩 1 个备份）/ 通过 37 次 ⇒ 该用例自带 **~7.5% flaky**。

★ 本文件为什么能**稳定**复现（而不是 49/50 的概率）
----------------------------------------------------------------------
把被测模块的**模块级 ``datetime``** 换成一个"永远返回同一时刻"的替身
⇒ 两次损坏算出的时间戳**必然相同** ⇒ 旧实现**稳定红**（只剩 1 个备份），
新实现**稳定绿**（2 个备份，互不覆盖）。
⇒ 不依赖概率 ⇒ 可以当长期护栏，而不是靠"多跑几次碰运气"。

★ 覆盖的四个模块（同一族缺陷、同一套命名规则）
----------------------------------------------------------------------
``profile_manager`` / ``history_manager`` / ``preset_manager`` / ``page_meta``

★ 不能被破坏的既有语义（本文件同时当回归护栏）
----------------------------------------------------------------------
- 备份名仍以 ``.corrupt-`` 开头（人工可识别）
- 无冲突时**保持原格式** ``<name>.corrupt-<YYYYMMDD-HHMMSS-ffffff>``
  （``tests/test_profile_manager.py::test_backup_name_contains_microseconds``
  的正则断言依赖它）
- 备份名里**仍看得出**损坏的大致时刻（可读性不能因为唯一性改造而丢掉）
- 「同一份损坏内容只备份一次」的去重语义不变
"""

import os
import re
import subprocess
import sys
from datetime import datetime as _real_datetime

import pytest

import history_manager
import page_meta
import preset_manager
import profile_manager


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 冻结时刻 —— 两次损坏必然算出同一个时间戳
_FROZEN = _real_datetime(2026, 1, 2, 3, 4, 5, 678901)
_FROZEN_STR = _FROZEN.strftime("%Y%m%d-%H%M%S-%f")


class _FrozenDatetime:
    """模块级 ``datetime`` 的替身：``now()`` 永远返回同一个时刻"""

    @staticmethod
    def now():
        return _FROZEN


def _freeze_clock(monkeypatch, module):
    """把被测模块的时钟**冻住** ⇒ 时间戳必然撞名（把概率问题变成必然问题）"""
    monkeypatch.setattr(module, "datetime", _FrozenDatetime, raising=True)


def _backups(directory, base):
    """目录里属于 ``base`` 的损坏备份文件名（已排序）"""
    return sorted(f for f in os.listdir(directory)
                  if f.startswith(base + ".corrupt-"))


def _write_bytes(path, data):
    with open(path, "wb") as f:
        f.write(data)


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# ── 1. profile_manager ────────────────────────────────────────


class TestProfileManagerBackupUniqueness:
    """设备档案损坏备份：两次损坏 ⇒ 两个备份，互不覆盖"""

    def _corrupt_twice(self, monkeypatch):
        path = profile_manager._profile_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _freeze_clock(monkeypatch, profile_manager)

        first = b'{"broken": [1, 2,'
        second = b'{"broken": [1, 2, 3, 4]'

        _write_bytes(path, first)
        profile_manager.get_profile("10.0.0.1")

        _write_bytes(path, second)
        # TTL 内存缓存要失效，否则第二次读的是缓存、根本不会重新读盘
        profile_manager._profiles_cache = None
        profile_manager._profiles_cache_time = 0
        profile_manager.get_profile("10.0.0.1")
        return path, first, second

    def test_two_corrupt_writes_same_timestamp_give_two_backups(self, monkeypatch):
        path, first, second = self._corrupt_twice(monkeypatch)
        directory = os.path.dirname(path)

        backups = _backups(directory, "profiles.json")
        assert len(backups) == 2, (
            "两次损坏必须留下两个备份（时间戳相同时也不能互相覆盖），实际: %r" % (backups,))

        contents = sorted(_read_bytes(os.path.join(directory, b)) for b in backups)
        assert contents == sorted([first, second]), (
            "两个备份必须各自保留原文（互不覆盖），实际: %r" % (contents,))


# ── 2. history_manager ────────────────────────────────────────


class TestHistoryManagerBackupUniqueness:
    """扫描历史损坏备份：两次损坏 ⇒ 两个备份，互不覆盖"""

    def test_two_corrupt_writes_same_timestamp_give_two_backups(self, tmp_config_dir, monkeypatch):
        path = history_manager._history_file()
        _freeze_clock(monkeypatch, history_manager)

        first = b'[{"broken": 1,'
        second = b'[{"broken": 1, 2, 3}'

        _write_bytes(path, first)
        history_manager.append({"device_ip": "1.2.3.4"})

        _write_bytes(path, second)
        history_manager.append({"device_ip": "1.2.3.5"})

        backups = _backups(tmp_config_dir, "history.json")
        assert len(backups) == 2, (
            "两次损坏必须留下两个备份（时间戳相同时也不能互相覆盖），实际: %r" % (backups,))

        contents = sorted(_read_bytes(os.path.join(tmp_config_dir, b)) for b in backups)
        assert contents == sorted([first, second]), (
            "两个备份必须各自保留原文（互不覆盖），实际: %r" % (contents,))


# ── 3. preset_manager ─────────────────────────────────────────


class TestPresetManagerBackupUniqueness:
    """预设损坏备份：两次损坏 ⇒ 两个备份，互不覆盖"""

    def test_two_corrupt_writes_same_timestamp_give_two_backups(self, tmp_config_dir, monkeypatch):
        path = preset_manager._presets_path()
        _freeze_clock(monkeypatch, preset_manager)

        first = b'[{"name": "x"'
        second = b'[{"name": "x", "y": 1}'

        _write_bytes(path, first)
        preset_manager.load_presets()

        _write_bytes(path, second)
        preset_manager.load_presets()

        backups = _backups(tmp_config_dir, "presets.json")
        assert len(backups) == 2, (
            "两次损坏必须留下两个备份（时间戳相同时也不能互相覆盖），实际: %r" % (backups,))

        contents = sorted(_read_bytes(os.path.join(tmp_config_dir, b)) for b in backups)
        assert contents == sorted([first, second]), (
            "两个备份必须各自保留原文（互不覆盖），实际: %r" % (contents,))


# ── 4. page_meta ──────────────────────────────────────────────


class TestPageMetaBackupUniqueness:
    """页元数据损坏备份：两次损坏 ⇒ 两个备份，互不覆盖"""

    def test_two_corrupt_writes_same_timestamp_give_two_backups(self, tmp_path, monkeypatch):
        root = str(tmp_path / "cache")
        os.makedirs(root, exist_ok=True)
        store = page_meta.PageMetaStore(root)
        _freeze_clock(monkeypatch, page_meta)

        first = b'{"j1": {"1":'
        second = b'{"j1": {"1": {"x": 1}}'

        _write_bytes(store._path, first)
        store.load()

        _write_bytes(store._path, second)
        store.load()

        backups = _backups(root, "page_meta.json")
        assert len(backups) == 2, (
            "两次损坏必须留下两个备份（时间戳相同时也不能互相覆盖），实际: %r" % (backups,))

        contents = sorted(_read_bytes(os.path.join(root, b)) for b in backups)
        assert contents == sorted([first, second]), (
            "两个备份必须各自保留原文（互不覆盖），实际: %r" % (contents,))


# ── 5. 跨进程：两个进程同时备份同一个损坏文件 ───────────────────

_CHILD_BACKUP_CODE = r'''
import sys
from datetime import datetime as _real_datetime

target = sys.argv[1]
sys.path.insert(0, sys.argv[2])

import history_manager


class _Frozen:
    @staticmethod
    def now():
        return _real_datetime(2026, 1, 2, 3, 4, 5, 678901)


history_manager.datetime = _Frozen
print(history_manager._backup_corrupt(target) or "")
'''


class TestCrossProcessBackupUniqueness:
    """
    两个**进程**各备份一次同一个损坏文件 ⇒ 必须两个备份。

    进程间的去重表互不相通 ⇒ 两边都会真的去建备份文件 ⇒ 唯一性必须
    由**文件名本身**保证（不能靠时钟分辨率）。
    """

    def test_two_processes_same_timestamp_give_two_backups(self, tmp_config_dir, monkeypatch):
        path = history_manager._history_file()
        original = b'[{"broken": 1,'
        _write_bytes(path, original)
        _freeze_clock(monkeypatch, history_manager)

        parent_backup = history_manager._backup_corrupt(path)
        assert parent_backup, "父进程必须成功备份"

        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_BACKUP_CODE, path, PROJECT_ROOT],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (
            "子进程备份失败: rc=%s stderr=%s" % (proc.returncode, proc.stderr))
        child_backup = proc.stdout.strip()
        assert child_backup, "子进程必须成功备份: %r" % (proc.stdout,)

        backups = _backups(tmp_config_dir, "history.json")
        assert len(backups) == 2, (
            "两个进程各备份一次 ⇒ 必须两个文件，实际: %r" % (backups,))
        assert parent_backup != child_backup, (
            "父子进程算出了同一个备份名: %r" % (parent_backup,))
        for b in backups:
            assert _read_bytes(os.path.join(tmp_config_dir, b)) == original, (
                "备份必须保留原文: %s" % b)


# ── 6. 既有语义回归护栏 ───────────────────────────────────────


class TestBackupNameKeepsLegacyShape:
    """唯一性改造不得改坏既有命名风格与人工可读性"""

    def test_single_backup_keeps_microsecond_format(self, tmp_config_dir, monkeypatch):
        """无冲突时备份名格式不变（既有断言 test_backup_name_contains_microseconds 依赖）"""
        path = history_manager._history_file()
        _write_bytes(path, b'[{"broken"')
        history_manager.append({"device_ip": "1.2.3.4"})

        backups = _backups(tmp_config_dir, "history.json")
        assert len(backups) == 1, "应当只有 1 个备份: %r" % (backups,)
        assert re.search(r"^history\.json\.corrupt-\d{8}-\d{6}-\d{6}$", backups[0]), (
            "备份名必须保持 <name>.corrupt-<微秒时间戳> 格式: %s" % backups[0])

    def test_backup_name_still_shows_when_corruption_happened(self, tmp_config_dir):
        """备份名里仍然看得出损坏的大致时刻（不能只留随机串）"""
        path = history_manager._history_file()
        before = _real_datetime.now()
        _write_bytes(path, b'[{"broken"')
        history_manager.append({"device_ip": "1.2.3.4"})
        after = _real_datetime.now()

        backups = _backups(tmp_config_dir, "history.json")
        assert len(backups) == 1
        name = backups[0]
        # 用 before/after 两个时刻兜住"跨零点/跨秒"的边界
        assert any(t.strftime("%Y%m%d") in name for t in (before, after)), (
            "备份名必须含日期（YYYYMMDD），便于人工判断何时损坏: %s" % name)
        assert any(t.strftime("%H%M%S") in name for t in (before, after)), (
            "备份名必须含时刻（HHMMSS），便于人工判断何时损坏: %s" % name)

    def test_collision_backup_keeps_corrupt_prefix_and_gets_index(self, tmp_config_dir, monkeypatch):
        """撞名时第二个备份仍以 .corrupt- 开头，并自动加序号（人工仍能认出是备份）"""
        path = history_manager._history_file()
        _freeze_clock(monkeypatch, history_manager)

        _write_bytes(path, b'[{"broken": 1,')
        history_manager.append({"device_ip": "1.2.3.4"})
        _write_bytes(path, b'[{"broken": 1, 2, 3}')
        history_manager.append({"device_ip": "1.2.3.5"})

        backups = _backups(tmp_config_dir, "history.json")
        assert len(backups) == 2
        for name in backups:
            assert name.startswith("history.json.corrupt-"), (
                "备份名必须仍以 .corrupt- 开头: %s" % name)
        # 撞名的那个带序号后缀
        assert any(name.endswith("-2") for name in backups), (
            "撞名时必须自动加序号: %r" % (backups,))


class TestDedupSemanticsPreserved:
    """「同一份损坏内容只备份一次」的去重语义不得被破坏"""

    def test_page_meta_same_corrupt_content_backed_up_once(self, tmp_path):
        root = str(tmp_path / "cache")
        os.makedirs(root, exist_ok=True)
        store = page_meta.PageMetaStore(root)

        _write_bytes(store._path, b"{not json")
        for _ in range(5):
            store.load()

        backups = _backups(root, "page_meta.json")
        assert len(backups) == 1, (
            "同一份损坏内容被反复备份: %r" % (backups,))

    def test_history_same_corrupt_content_backed_up_once(self, tmp_config_dir):
        path = history_manager._history_file()
        _write_bytes(path, b'[{"broken"')
        for _ in range(5):
            history_manager._load()

        backups = _backups(tmp_config_dir, "history.json")
        assert len(backups) == 1, (
            "同一份损坏内容被反复备份: %r" % (backups,))
