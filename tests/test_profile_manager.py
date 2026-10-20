"""
profile_manager 单元测试 — HP Scan Tool v4.0

覆盖：保存/读取/删除/清空 / 损坏文件保护（N-4）/ 跨进程并发不丢更新

★ 损坏文件保护（N-4，与 history_manager / preset_manager / page_meta 同一族）
   profiles.json 损坏时：
   - 读侧降级为 {}，**不抛异常**
   - 同目录生成 <name>.corrupt-<微秒时间戳> 备份，原文逐字节保留
   - save_profile **拒绝覆盖**损坏原文（返回 False），原文可从备份恢复
   - "读不出来"（OSError / 真实独占锁）时同样拒绝写入
     ⇒ 绝不用空状态覆盖一个我们根本读不到的文件

⚠ 并发用例用**两个真实子进程**（msvcrt 字节区间锁是进程级的，线程测不出
  跨进程行为）。
"""

import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import time

import pytest

import profile_manager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程写脚本：用 APPDATA 把配置目录重定向到临时目录，走真实代码路径
_PROFILE_WRITER = r'''
import os, sys, time
appdata, project_root, tag, rounds, start_at = sys.argv[1:6]
os.environ["APPDATA"] = appdata
sys.path.insert(0, project_root)
import profile_manager
while time.time() < float(start_at):
    time.sleep(0.005)
for i in range(int(rounds)):
    profile_manager.save_profile("%s-%d" % (tag, i), {"tag": tag, "seq": i})
'''


@pytest.fixture
def prof_dir(tmp_path, monkeypatch):
    """设备档案目录重定向到临时目录（不碰真实 %APPDATA%），并重置模块缓存"""
    appdata = tmp_path / "appdata"
    cfg = appdata / "HP_Scan_Tool"
    cfg.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(profile_manager, "_app_dir", str(cfg), raising=False)
    monkeypatch.setattr(profile_manager, "_profiles_cache", None, raising=False)
    monkeypatch.setattr(profile_manager, "_profiles_cache_time", 0, raising=False)
    monkeypatch.setattr(profile_manager, "_CORRUPT_BACKED_UP", set(), raising=False)
    return str(cfg)


def _profile_file(prof_dir):
    return os.path.join(prof_dir, "profiles.json")


def _backups(prof_dir):
    """目录里的损坏备份文件列表"""
    return sorted(f for f in os.listdir(prof_dir) if ".corrupt-" in f)


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _expire_cache():
    """让下一次读真正落盘（等价于 TTL 过期）——TTL 内的读走内存缓存，不重读盘"""
    profile_manager._profiles_cache = None
    profile_manager._profiles_cache_time = 0


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def _exclusive_handle(path):
    """真实独占句柄打开文件（dwShareMode=0）⇒ 其它进程 open() 会 PermissionError"""
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = ctypes.c_void_p
    handle = k32.CreateFileW(
        ctypes.c_wchar_p(path),
        0x80000000,   # GENERIC_READ
        0,            # dwShareMode = FILE_SHARE_NONE（独占）
        None,
        3,            # OPEN_EXISTING
        0x80,         # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise OSError("CreateFileW 失败: %s" % path)
    return handle


# ── 正常路径 ────────────────────────────────────────────────


class TestNormalPath:
    def test_save_and_get_roundtrip(self, prof_dir):
        assert profile_manager.save_profile("10.0.0.1", {"res": 300, "fmt": "pdf"}) is True
        assert profile_manager.get_profile("10.0.0.1") == {"res": 300, "fmt": "pdf"}
        assert profile_manager.get_all_profiles() == {"10.0.0.1": {"res": 300, "fmt": "pdf"}}

    def test_get_unknown_ip_returns_empty(self, prof_dir):
        assert profile_manager.get_profile("10.0.0.99") == {}
        assert profile_manager.get_profile("") == {}

    def test_delete_profile(self, prof_dir):
        profile_manager.save_profile("10.0.0.1", {"res": 300})
        profile_manager.save_profile("10.0.0.2", {"res": 600})
        assert profile_manager.delete_profile("10.0.0.1") is True
        assert profile_manager.get_profile("10.0.0.1") == {}
        assert "10.0.0.2" in profile_manager.get_all_profiles()

    def test_clear_all(self, prof_dir):
        profile_manager.save_profile("10.0.0.1", {"res": 300})
        profile_manager.save_profile("10.0.0.2", {"res": 600})
        assert profile_manager.clear_all() is True
        assert profile_manager.get_all_profiles() == {}

    def test_normal_path_leaves_no_backup(self, prof_dir, caplog):
        with caplog.at_level(logging.ERROR):
            profile_manager.save_profile("10.0.0.1", {"res": 300})
            profile_manager.get_profile("10.0.0.1")
            profile_manager.delete_profile("10.0.0.1")
        assert _backups(prof_dir) == []
        assert _errors(caplog) == []

    def test_missing_file_is_not_treated_as_corrupt(self, prof_dir, caplog):
        assert not os.path.exists(_profile_file(prof_dir))
        with caplog.at_level(logging.ERROR):
            assert profile_manager.get_profile("10.0.0.1") == {}
        assert _backups(prof_dir) == []
        assert _errors(caplog) == []


# ── 损坏文件保护（N-4）──────────────────────────────────────


class TestCorruptProtection:
    """profiles.json 损坏后，原文必须被备份下来，不能被静默清空"""

    def test_corrupt_file_degrades_to_empty_without_raising(self, prof_dir):
        path = _profile_file(prof_dir)
        with open(path, "wb") as f:
            f.write(b'{"broken": [1, 2,')

        assert profile_manager.get_profile("10.0.0.1") == {}
        assert profile_manager.get_all_profiles() == {}

    def test_corrupt_file_is_backed_up_with_original_bytes(self, prof_dir):
        path = _profile_file(prof_dir)
        original = b'{"broken": [1, 2,'
        with open(path, "wb") as f:
            f.write(original)

        profile_manager.get_profile("10.0.0.1")  # 触发损坏检测

        backups = _backups(prof_dir)
        assert backups, "损坏的设备档案必须留下备份"
        assert _read_bytes(os.path.join(prof_dir, backups[0])) == original
        assert _read_bytes(path) == original, "原文件必须逐字节未变"

    def test_backup_name_contains_microseconds(self, prof_dir):
        path = _profile_file(prof_dir)
        with open(path, "wb") as f:
            f.write(b"corrupt-1")

        profile_manager.get_profile("10.0.0.1")

        backups = _backups(prof_dir)
        assert len(backups) == 1
        assert re.search(r"^profiles\.json\.corrupt-\d{8}-\d{6}-\d{6}$", backups[0]), \
            "备份名必须带微秒时间戳: %s" % backups[0]

    def test_two_corrupt_writes_produce_two_different_backups(self, prof_dir):
        path = _profile_file(prof_dir)
        with open(path, "wb") as f:
            f.write(b"corrupt-first")
        profile_manager.get_profile("10.0.0.1")
        with open(path, "wb") as f:
            f.write(b"corrupt-second-longer")
        _expire_cache()
        profile_manager.get_profile("10.0.0.1")

        backups = _backups(prof_dir)
        assert len(backups) == 2, "两次不同的损坏内容应各留一个备份: %s" % backups
        assert backups[0] != backups[1]

    def test_corrupt_file_logs_error(self, prof_dir, caplog):
        path = _profile_file(prof_dir)
        with open(path, "wb") as f:
            f.write(b"corrupt")
        with caplog.at_level(logging.ERROR):
            profile_manager.get_profile("10.0.0.1")
        errors = _errors(caplog)
        assert errors, "损坏必须记 error 日志"
        assert any(r.name == "profile_manager" for r in errors)

    def test_save_profile_refuses_to_overwrite_corrupt_file(self, prof_dir, caplog):
        path = _profile_file(prof_dir)
        original = b'{"corrupt": [1, 2,'
        with open(path, "wb") as f:
            f.write(original)

        with caplog.at_level(logging.ERROR):
            assert profile_manager.save_profile("10.0.0.1", {"res": 300}) is False

        assert _read_bytes(path) == original, "损坏原文不能被覆盖"
        backups = _backups(prof_dir)
        assert backups, "必须留有备份，否则原文无法恢复"
        assert _read_bytes(os.path.join(prof_dir, backups[0])) == original, "备份必须能恢复原文"

    def test_delete_profile_refuses_on_corrupt_file(self, prof_dir):
        path = _profile_file(prof_dir)
        original = b"not json at all"
        with open(path, "wb") as f:
            f.write(original)
        assert profile_manager.delete_profile("10.0.0.1") is False
        assert _read_bytes(path) == original

    def test_clear_all_on_corrupt_file_keeps_backup(self, prof_dir):
        path = _profile_file(prof_dir)
        original = b"not json at all"
        with open(path, "wb") as f:
            f.write(original)
        assert profile_manager.clear_all() is True
        backups = _backups(prof_dir)
        assert backups, "清空前损坏原文必须已备份"
        assert _read_bytes(os.path.join(prof_dir, backups[0])) == original

    def test_top_level_not_object_is_treated_as_corrupt(self, prof_dir):
        path = _profile_file(prof_dir)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(["not", "an", "object"], f)
        assert profile_manager.get_all_profiles() == {}
        assert _backups(prof_dir), "顶层不是对象 ⇒ 同样属于损坏，必须备份"

    def test_binary_garbage_is_treated_as_corrupt(self, prof_dir):
        path = _profile_file(prof_dir)
        with open(path, "wb") as f:
            f.write(b"\xff\xfe\x00\x01garbage")
        assert profile_manager.get_all_profiles() == {}
        assert _backups(prof_dir)


# ── 读不出来（OSError）≠ 文件为空 ───────────────────────────


class TestUnreadableFile:
    """读不出来（被独占）时：读侧降级为 {}，写侧**拒绝**用空状态覆盖"""

    def test_get_on_exclusively_locked_file_returns_empty(self, prof_dir):
        path = _profile_file(prof_dir)
        with open(path, "wb") as f:
            f.write(b'{"10.0.0.1": {"res": 300}}')
        handle = _exclusive_handle(path)
        try:
            assert profile_manager.get_profile("10.0.0.1") == {}
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def test_save_refuses_when_file_cannot_be_read(self, prof_dir, caplog):
        path = _profile_file(prof_dir)
        original = b'{"10.0.0.1": {"res": 300}}'
        with open(path, "wb") as f:
            f.write(original)

        handle = _exclusive_handle(path)
        try:
            with caplog.at_level(logging.ERROR):
                assert profile_manager.save_profile("10.0.0.2", {"res": 600}) is False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

        assert _read_bytes(path) == original, "读不出来时绝不能用空状态覆盖原文件"
        assert _errors(caplog), "读失败必须记 error 日志"

    def test_delete_refuses_when_file_cannot_be_read(self, prof_dir):
        path = _profile_file(prof_dir)
        original = b'{"10.0.0.1": {"res": 300}}'
        with open(path, "wb") as f:
            f.write(original)
        handle = _exclusive_handle(path)
        try:
            assert profile_manager.delete_profile("10.0.0.1") is False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        assert _read_bytes(path) == original

    def test_clear_all_refuses_when_file_cannot_be_read(self, prof_dir):
        path = _profile_file(prof_dir)
        original = b'{"10.0.0.1": {"res": 300}}'
        with open(path, "wb") as f:
            f.write(original)
        handle = _exclusive_handle(path)
        try:
            assert profile_manager.clear_all() is False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        assert _read_bytes(path) == original


# ── 跨进程并发（两个真实子进程）──────────────────────────────


class TestCrossProcessConcurrency:
    """msvcrt 锁是进程级的 ⇒ 必须用真实子进程验证"""

    def _run_writers(self, appdata, tags, rounds):
        start_at = time.time() + 0.6
        procs = []
        for tag in tags:
            procs.append((tag, subprocess.Popen(
                [sys.executable, "-c", _PROFILE_WRITER, appdata, PROJECT_ROOT,
                 tag, str(rounds), str(start_at)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )))
        results = []
        for tag, p in procs:
            so, se = p.communicate(timeout=180)
            results.append((tag, p.returncode,
                            so.decode("utf-8", "replace"),
                            se.decode("utf-8", "replace")))
        return results

    def test_two_processes_no_lost_update(self, tmp_path):
        appdata = tmp_path / "appdata"
        (appdata / "HP_Scan_Tool").mkdir(parents=True, exist_ok=True)
        rounds = 20

        results = self._run_writers(str(appdata), ["A", "B"], rounds)
        for tag, rc, so, se in results:
            assert rc == 0, "写进程 %s 失败: %s" % (tag, se)

        path = str(appdata / "HP_Scan_Tool" / "profiles.json")
        with open(path, "r", encoding="utf-8") as f:
            profiles = json.load(f)
        assert len(profiles) == 2 * rounds
        assert set(profiles) == {"%s-%d" % (t, i)
                                 for t in ("A", "B") for i in range(rounds)}

    def test_concurrent_writes_leave_valid_json_and_no_tmp(self, tmp_path):
        appdata = tmp_path / "appdata"
        (appdata / "HP_Scan_Tool").mkdir(parents=True, exist_ok=True)
        results = self._run_writers(str(appdata), ["A", "B"], 5)
        for tag, rc, so, se in results:
            assert rc == 0, se
        cfg = appdata / "HP_Scan_Tool"
        leftovers = [f for f in os.listdir(str(cfg)) if f.endswith(".tmp")]
        assert leftovers == [], "并发写结束后不能有 .tmp 残留"
