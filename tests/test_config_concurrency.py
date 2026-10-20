"""
配置并发写 / 写失败保护测试 — HP Scan Tool v4.0
覆盖: 唯一临时文件 / 跨进程锁 / 原子替换 / 写失败有日志不抛异常 / 临时文件清理
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time

import pytest

import cache_manager
import config_io
import history_manager
import profile_manager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程写脚本：用 APPDATA 把配置目录重定向到临时目录，走真实代码路径
# start_at 是统一的起跑时刻（墙钟），让多个子进程的写事务真正重叠
_CACHE_WRITER = r'''
import os, sys, time
appdata, project_root, tag, rounds, size, start_at = sys.argv[1:7]
os.environ["APPDATA"] = appdata
sys.path.insert(0, project_root)
import cache_manager
while time.time() < float(start_at):
    time.sleep(0.005)
for i in range(int(rounds)):
    cache_manager.save_cache_config({"tag": tag, "seq": i, "blob": tag * int(size)})
'''

_HISTORY_WRITER = r'''
import os, sys, time
appdata, project_root, tag, rounds, size, start_at = sys.argv[1:7]
os.environ["APPDATA"] = appdata
sys.path.insert(0, project_root)
import history_manager
while time.time() < float(start_at):
    time.sleep(0.005)
for i in range(int(rounds)):
    history_manager.append({"tag": tag, "seq": i})
'''

_PROFILE_WRITER = r'''
import os, sys, time
appdata, project_root, tag, rounds, size, start_at = sys.argv[1:7]
os.environ["APPDATA"] = appdata
sys.path.insert(0, project_root)
import profile_manager
while time.time() < float(start_at):
    time.sleep(0.005)
for i in range(int(rounds)):
    profile_manager.save_profile("%s-%d" % (tag, i), {"tag": tag, "seq": i})
'''


def _appdata_dir(tmp_path):
    """独立 APPDATA 目录：子进程据此定位 %APPDATA%\\HP_Scan_Tool"""
    appdata = tmp_path / "appdata"
    appdata.mkdir(parents=True, exist_ok=True)
    return str(appdata)


def _cfg_dir(appdata):
    return os.path.join(appdata, "HP_Scan_Tool")


def _tmp_leftovers(directory):
    return [f for f in os.listdir(directory) if f.endswith(".tmp")]


def _run_writers(script, appdata, tags, rounds, size):
    """并发拉起多个子进程写同一份配置，返回 [(tag, returncode, stdout, stderr)]"""
    start_at = time.time() + 0.6  # 统一起跑时刻：让各进程的写事务真正重叠
    procs = []
    for tag in tags:
        procs.append((tag, subprocess.Popen(
            [sys.executable, "-c", script, appdata, PROJECT_ROOT, tag,
             str(rounds), str(size), str(start_at)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )))
    results = []
    for tag, p in procs:
        so, se = p.communicate(timeout=180)
        results.append((tag, p.returncode,
                        so.decode("utf-8", "replace"), se.decode("utf-8", "replace")))
    return results


# ── 唯一临时文件 + 原子替换 ─────────────────────────────────


class TestAtomicWrite:
    def test_temp_file_names_are_unique(self, tmp_path, monkeypatch):
        """两次写必须用两个不同的临时名，否则并发写会互相覆盖"""
        target = str(tmp_path / "cfg.json")
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(src)
            return real_replace(src, dst)

        monkeypatch.setattr(cache_manager.os, "replace", spy)
        cache_manager._atomic_write_json(target, {"a": 1})
        cache_manager._atomic_write_json(target, {"b": 2})

        assert len(seen) == 2
        assert seen[0] != seen[1], "临时文件名必须唯一"
        assert all(p.endswith(".tmp") for p in seen)
        assert all(os.path.dirname(p) == str(tmp_path) for p in seen)

    def test_no_tmp_left_after_success(self, tmp_path):
        target = str(tmp_path / "cfg.json")
        cache_manager._atomic_write_json(target, {"a": 1})
        assert _tmp_leftovers(tmp_path) == []
        with open(target, "r", encoding="utf-8") as f:
            assert json.load(f) == {"a": 1}

    def test_no_tmp_left_after_failure(self, tmp_path, monkeypatch):
        """写失败时临时文件必须被清理"""
        target = str(tmp_path / "cfg.json")

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(cache_manager.os, "replace", boom)
        with pytest.raises(OSError):
            cache_manager._atomic_write_json(target, {"a": 1})
        assert _tmp_leftovers(tmp_path) == []


# ── 跨进程锁 ────────────────────────────────────────────────


class TestFileLock:
    def test_lock_file_persists(self, tmp_path):
        """锁文件是持久文件（不删除），只锁它的字节区间"""
        target = str(tmp_path / "cfg.json")
        with cache_manager._file_lock(target) as acquired:
            assert acquired is True
        assert (tmp_path / "cfg.json.lock").exists()
        # 再取一次锁，锁文件仍在
        with cache_manager._file_lock(target):
            pass
        assert (tmp_path / "cfg.json.lock").exists()

    def test_lock_is_exclusive(self, tmp_path):
        """持锁期间，另一个句柄拿不到同一字节区间的锁"""
        import msvcrt

        target = str(tmp_path / "cfg.json")
        with cache_manager._file_lock(target):
            fd = os.open(target + ".lock", os.O_RDWR | os.O_CREAT)
            try:
                with pytest.raises(OSError):
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            finally:
                os.close(fd)

    def test_other_process_waits_for_lock(self, tmp_path):
        """另一个进程必须等锁释放后才拿得到（跨进程串行化）"""
        target = str(tmp_path / "cfg.json")
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import cache_manager\n"
            "print('STARTED', flush=True)\n"
            "with cache_manager._file_lock(sys.argv[2]) as ok:\n"
            "    print('ACQUIRED' if ok else 'TIMEOUT', flush=True)\n"
        )
        with cache_manager._file_lock(target):
            proc = subprocess.Popen(
                [sys.executable, "-c", script, PROJECT_ROOT, target],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            assert proc.stdout.readline().strip() == b"STARTED"
            time.sleep(0.5)
            assert proc.poll() is None, "另一个进程应被锁挡住"
        out, err = proc.communicate(timeout=60)
        assert b"ACQUIRED" in out, f"子进程未拿到锁: {out!r} / {err!r}"


# ── 跨进程并发写 ────────────────────────────────────────────


class TestConcurrentProcesses:
    def test_two_processes_cache_config_final_is_one_complete_result(self, tmp_path):
        """两个进程并发写同一配置文件：最终内容是其中一个完整的结果"""
        appdata = _appdata_dir(tmp_path)
        rounds, size = 5, 20000
        results = _run_writers(_CACHE_WRITER, appdata, ["A", "B"], rounds, size)
        for tag, rc, so, se in results:
            assert rc == 0, f"写进程 {tag} 失败: {se}"

        cfg_path = os.path.join(_cfg_dir(appdata), "cache_config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)  # 必须能解析：不是混合、不是空、不是损坏的 JSON

        assert data["tag"] in ("A", "B")
        assert data["blob"] == data["tag"] * size, "最终内容必须来自同一个写者"
        assert data["seq"] == rounds - 1

    def test_two_processes_history_append_no_lost_update(self, tmp_path):
        """两个进程并发追加历史：读-改-写全程持锁，一条都不能丢"""
        appdata = _appdata_dir(tmp_path)
        rounds = 20
        results = _run_writers(_HISTORY_WRITER, appdata, ["A", "B"], rounds, 0)
        for tag, rc, so, se in results:
            assert rc == 0, f"写进程 {tag} 失败: {se}"

        hist_path = os.path.join(_cfg_dir(appdata), "history.json")
        with open(hist_path, "r", encoding="utf-8") as f:
            records = json.load(f)

        assert len(records) == 2 * rounds
        assert {(r["tag"], r["seq"]) for r in records} == {
            (t, i) for t in ("A", "B") for i in range(rounds)
        }

    def test_two_processes_profiles_no_lost_update(self, tmp_path):
        """两个进程并发保存配置：不能互相覆盖"""
        appdata = _appdata_dir(tmp_path)
        rounds = 20
        results = _run_writers(_PROFILE_WRITER, appdata, ["A", "B"], rounds, 0)
        for tag, rc, so, se in results:
            assert rc == 0, f"写进程 {tag} 失败: {se}"

        prof_path = os.path.join(_cfg_dir(appdata), "profiles.json")
        with open(prof_path, "r", encoding="utf-8") as f:
            profiles = json.load(f)

        assert len(profiles) == 2 * rounds
        assert set(profiles) == {f"{t}-{i}" for t in ("A", "B") for i in range(rounds)}

    def test_no_tmp_left_after_concurrent_writes(self, tmp_path):
        """并发写结束后目录里不能有残留 .tmp"""
        appdata = _appdata_dir(tmp_path)
        results = _run_writers(_CACHE_WRITER, appdata, ["A", "B"], 3, 1000)
        for tag, rc, so, se in results:
            assert rc == 0, f"写进程 {tag} 失败: {se}"
        assert _tmp_leftovers(_cfg_dir(appdata)) == []


# ── 线程并发写 ──────────────────────────────────────────────


class TestConcurrentThreads:
    def test_two_threads_cache_config_final_is_valid(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "cache_config.json"
        monkeypatch.setattr(cache_manager, "_cache_config_path", lambda: str(cfg_path))
        monkeypatch.setattr(cache_manager, "_config_cache", None)
        monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
        errors = []

        def writer(tag):
            try:
                for i in range(20):
                    cache_manager.save_cache_config({"tag": tag, "seq": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["tag"] in ("A", "B")
        assert _tmp_leftovers(tmp_path) == []

    def test_two_threads_history_append_no_lost_update(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history_manager, "_history_file",
                            lambda: str(tmp_path / "history.json"))
        errors = []

        def writer(tag):
            try:
                for i in range(10):
                    history_manager.append({"tag": tag, "seq": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        with open(tmp_path / "history.json", "r", encoding="utf-8") as f:
            records = json.load(f)
        assert len(records) == 20
        assert _tmp_leftovers(tmp_path) == []


# ── 写失败：不崩，但要有日志 ────────────────────────────────


class TestWriteFailure:
    def test_cache_config_write_failure_logs_error(self, tmp_path, monkeypatch, caplog):
        cfg_path = tmp_path / "cache_config.json"
        monkeypatch.setattr(cache_manager, "_cache_config_path", lambda: str(cfg_path))

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(cache_manager.os, "replace", boom)
        with caplog.at_level(logging.ERROR):
            cache_manager.save_cache_config({"a": 1})  # 不抛异常

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "写失败必须记 error 日志"
        assert any(r.name == "cache_manager" for r in errors)
        assert _tmp_leftovers(tmp_path) == []

    def test_cache_config_write_failure_keeps_old_file(self, tmp_path, monkeypatch, caplog):
        cfg_path = tmp_path / "cache_config.json"
        monkeypatch.setattr(cache_manager, "_cache_config_path", lambda: str(cfg_path))
        cache_manager.save_cache_config({"old": True})
        with open(cfg_path, "r", encoding="utf-8") as f:
            before = f.read()

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(cache_manager.os, "replace", boom)
        with caplog.at_level(logging.ERROR):
            cache_manager.save_cache_config({"new": True})  # 不抛异常

        with open(cfg_path, "r", encoding="utf-8") as f:
            assert f.read() == before, "写失败时旧文件必须原样保留"
        assert [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_cache_config_write_failure_mid_dump_cleans_tmp(self, tmp_path, monkeypatch, caplog):
        """写到一半失败（fsync 报错）时，临时文件同样要清掉"""
        cfg_path = tmp_path / "cache_config.json"
        monkeypatch.setattr(cache_manager, "_cache_config_path", lambda: str(cfg_path))

        def boom(fd):
            raise OSError("io error")

        monkeypatch.setattr(cache_manager.os, "fsync", boom)
        with caplog.at_level(logging.ERROR):
            cache_manager.save_cache_config({"a": 1})  # 不抛异常

        assert [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert _tmp_leftovers(tmp_path) == []

    def test_history_write_failure_logs_error(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(history_manager, "_history_file",
                            lambda: str(tmp_path / "history.json"))

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(history_manager.os, "replace", boom)
        with caplog.at_level(logging.ERROR):
            history_manager.append({"a": 1})  # 不抛异常

        assert [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert _tmp_leftovers(tmp_path) == []

    def test_profile_write_failure_logs_error(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(profile_manager, "_profile_path",
                            lambda: str(tmp_path / "profiles.json"))
        monkeypatch.setattr(profile_manager, "_profiles_cache", None)

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(profile_manager.os, "replace", boom)
        with caplog.at_level(logging.ERROR):
            profile_manager.save_profile("1.2.3.4", {"res": 1200})  # 不抛异常

        assert [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert _tmp_leftovers(tmp_path) == []


# ── config_io 共享模块本身 ──────────────────────────────────


class TestConfigIo:
    """直接验证共享模块 config_io 的三个原语（收口后的唯一实现）"""

    def test_config_dir_uses_appdata(self, tmp_path, monkeypatch):
        """配置目录取 %APPDATA%\\HP_Scan_Tool\\，并确保目录存在"""
        monkeypatch.setenv("APPDATA", str(tmp_path))
        d = config_io.config_dir()
        assert d == os.path.join(str(tmp_path), "HP_Scan_Tool")
        assert os.path.isdir(d)

    def test_atomic_write_json_content_and_no_tmp(self, tmp_path):
        """写成功后文件内容正确，且临时文件已被清理"""
        target = str(tmp_path / "cfg.json")
        config_io.atomic_write_json(target, {"a": 1, "中文": "值"})

        with open(target, "r", encoding="utf-8") as f:
            assert json.load(f) == {"a": 1, "中文": "值"}
        assert _tmp_leftovers(tmp_path) == [], "临时文件必须已清理"

    def test_atomic_write_json_keeps_old_file_on_failure(self, tmp_path, monkeypatch):
        """写失败时旧文件原样保留，临时文件被清理"""
        target = str(tmp_path / "cfg.json")
        config_io.atomic_write_json(target, {"old": True})
        with open(target, "r", encoding="utf-8") as f:
            before = f.read()

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(config_io.os, "replace", boom)
        with pytest.raises(OSError):
            config_io.atomic_write_json(target, {"new": True})

        with open(target, "r", encoding="utf-8") as f:
            assert f.read() == before, "写失败时旧文件必须原样保留"
        assert _tmp_leftovers(tmp_path) == []

    def test_config_lock_is_mutually_exclusive(self, tmp_path):
        """两个线程同时进临界区：绝不重叠"""
        target = str(tmp_path / "cfg.json")
        state = {"inside": 0, "max_inside": 0}
        guard = threading.Lock()

        def worker():
            with config_io.config_lock(target):
                with guard:
                    state["inside"] += 1
                    state["max_inside"] = max(state["max_inside"], state["inside"])
                time.sleep(0.2)
                with guard:
                    state["inside"] -= 1

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert state["max_inside"] == 1, "临界区重叠：跨进程锁没起作用"

    def test_config_lock_file_persists_after_use(self, tmp_path):
        """锁的是字节区间，不是文件生命周期：用完之后锁文件仍然存在"""
        target = str(tmp_path / "cfg.json")

        with config_io.config_lock(target) as acquired:
            assert acquired is True
        assert (tmp_path / "cfg.json.lock").exists()

        # 再取一次锁，锁文件仍在
        with config_io.config_lock(target) as acquired:
            assert acquired is True
        assert (tmp_path / "cfg.json.lock").exists()
