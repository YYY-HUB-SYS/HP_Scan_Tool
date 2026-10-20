"""
严格锁调用点回归测试 — HP Scan Tool v4.0（波次 2：7 处锁调用点切到严格锁）

覆盖 brief §3 的 7 条必做回归测试：
  1. 两个**真实子进程**并发写同一个配置（cache_config / history / profiles / scan_config）
     ⇒ 产出始终是合法 JSON；读-改-写的键**一条都不能丢**
  2. 持锁方 sleep 超过等待上限 ⇒ 另一方**不写入**（显式拒绝），原文件内容不变，且有 error 日志
  3. ConfigLockTimeout **不逃逸**成未捕获异常（函数返回 False 而不是抛）
  4. history_manager 读失败（mock open 抛 OSError）⇒ 仍先备份、**不覆盖**原历史（F-7 护栏没被改坏）
  5. history_manager 正常 append 后条数增长
  6. profile_manager 的 save / delete / clear_all 正常路径
  7. 进程内 _config_lock 仍在（同进程并发写也不丢更新）

⚠️ 并发用例一律用**两个真实子进程**（subprocess 调 sys.executable）：
   msvcrt 的字节区间锁是**进程级**的，线程测不出跨进程行为。
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
import scan_coordinator

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 父进程等待锁的上限（秒）——测试里调小，免得每次等 30 秒
_SHORT_WAIT = 0.5

# ── 子进程脚本 ────────────────────────────────────────────

# 持锁不放：逼另一个进程走到"等待上限"⇒ 走 ConfigLockTimeout 分支
_HOLD_LOCK = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
import config_io
target, hold = sys.argv[2], float(sys.argv[3])
with config_io.config_write_lock(target):
    print("HOLDING", flush=True)
    time.sleep(hold)
'''

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

_SCAN_CONFIG_WRITER = r'''
import os, sys, time
appdata, project_root, tag, rounds, size, start_at = sys.argv[1:7]
os.environ["APPDATA"] = appdata
sys.path.insert(0, project_root)
import scan_coordinator
while time.time() < float(start_at):
    time.sleep(0.005)
for i in range(int(rounds)):
    scan_coordinator.save_config({"tag": tag, "seq": i, "blob": tag * int(size)})
'''


# ── helpers ───────────────────────────────────────────────


def _appdata_dir(tmp_path):
    """独立 APPDATA 目录：子进程据此定位 %APPDATA%\\HP_Scan_Tool"""
    appdata = tmp_path / "appdata"
    appdata.mkdir(parents=True, exist_ok=True)
    return str(appdata)


def _cfg_dir(appdata):
    return os.path.join(appdata, "HP_Scan_Tool")


def _run_writers(script, appdata, tags, rounds, size=0):
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


def _hold_lock(target, hold=10.0):
    """起一个子进程持住 <target>.lock 不放（跨进程真锁）"""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, PROJECT_ROOT, target, str(hold)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    line = proc.stdout.readline().strip()
    assert line == b"HOLDING", f"子进程没拿到锁: {line!r}"
    return proc


def _errors(caplog, logger_name):
    """指定 logger 记的 error 日志"""
    return [r for r in caplog.records
            if r.levelno >= logging.ERROR and r.name == logger_name]


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── 1. 静态检查：7 个调用点全部切到严格锁 ────────────────────


class TestCallSitesSwitched:
    """防止以后有人把调用点改回宽松锁（那会退回"未持锁还写入"= 丢更新）"""

    _EXPECTED = {
        "cache_manager.py": 1,
        "history_manager.py": 2,
        "profile_manager.py": 3,
        "scan_coordinator.py": 1,
    }

    def _source(self, name):
        path = os.path.join(PROJECT_ROOT, name)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_all_seven_call_sites_use_strict_lock(self):
        total = 0
        for name, expected in self._EXPECTED.items():
            src = self._source(name)
            n = src.count("config_io.config_write_lock(")
            assert n == expected, f"{name} 严格锁调用点数应为 {expected}，实际 {n}"
            total += n
        assert total == 7, "7 个生产调用点必须全部切到严格锁"

    def test_no_loose_lock_left_in_those_modules(self):
        for name in self._EXPECTED:
            src = self._source(name)
            assert "_file_lock(" not in src, f"{name} 仍残留宽松锁调用 _file_lock("
            assert "config_io.config_lock(" not in src, f"{name} 仍残留宽松锁 config_lock("

    def test_config_io_default_semantics_untouched(self):
        """config_io 的默认 required=False 行为不能被本波次改掉"""
        src = self._source("config_io.py")
        assert "def config_lock(path: str, timeout: float = None, required: bool = False):" in src
        assert "def config_write_lock(path: str, timeout: float = None):" in src


# ── 2 + 3. 拿不到锁 ⇒ 显式拒绝（不写入 / 返回 False / 有 error 日志）──


class TestRefusesWriteWhenLockHeld:
    """
    持锁方 sleep 超过等待上限 ⇒ 另一方**不写入**：
      - 函数返回 False（ConfigLockTimeout 不逃逸成未捕获异常）
      - 原文件内容**逐字节不变**（不是"覆盖成新值"）
      - 记 error 日志（能让人知道这次没存上）
    """

    @pytest.fixture(autouse=True)
    def _short_wait(self, monkeypatch):
        monkeypatch.setattr(config_io, "_LOCK_WAIT_SECONDS", _SHORT_WAIT)

    @staticmethod
    def _release(proc):
        proc.kill()
        proc.wait(timeout=30)

    # ── 调用点 1：cache_manager.save_cache_config ──
    def test_cache_manager_save_cache_config_refuses_write(self, tmp_path, monkeypatch, caplog):
        cfg = tmp_path / "cache_config.json"
        monkeypatch.setattr(cache_manager, "_cache_config_path", lambda: str(cfg))
        monkeypatch.setattr(cache_manager, "_config_cache", None)
        monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
        cfg.write_text(json.dumps({"old": True}), encoding="utf-8")
        before = _read(cfg)

        proc = _hold_lock(str(cfg))
        try:
            with caplog.at_level(logging.ERROR):
                result = cache_manager.save_cache_config({"new": True})
        finally:
            self._release(proc)

        assert result is False, "拿不到锁必须返回 False（不能抛异常、不能当成功）"
        assert _read(cfg) == before, "原文件必须原样保留"
        assert _errors(caplog, "cache_manager"), "必须记 error 日志"

    # ── 调用点 2：history_manager.append（读-改-写事务）──
    def test_history_append_refuses_write_and_keeps_history(self, tmp_path, monkeypatch, caplog):
        hp = tmp_path / "history.json"
        monkeypatch.setattr(history_manager, "_history_file", lambda: str(hp))
        history_manager.append({"device_ip": "1.2.3.4"})
        before = _read(hp)

        proc = _hold_lock(str(hp))
        try:
            with caplog.at_level(logging.ERROR):
                result = history_manager.append({"device_ip": "5.6.7.8"})
        finally:
            self._release(proc)

        assert result is False
        # ★ 最关键的一条：不能拿"空历史"覆盖原历史（F-7 场景）
        assert _read(hp) == before, "原历史必须完整保留，不能被空列表/单条覆盖"
        assert len(json.loads(_read(hp))) == 1
        assert _errors(caplog, "history_manager")

    # ── 调用点 3：history_manager.clear ──
    def test_history_clear_refuses_write(self, tmp_path, monkeypatch, caplog):
        hp = tmp_path / "history.json"
        monkeypatch.setattr(history_manager, "_history_file", lambda: str(hp))
        history_manager.append({"device_ip": "1.2.3.4"})
        before = _read(hp)

        proc = _hold_lock(str(hp))
        try:
            with caplog.at_level(logging.ERROR):
                result = history_manager.clear()
        finally:
            self._release(proc)

        assert result is False
        assert _read(hp) == before, "拿不到锁时不能清空历史"
        assert _errors(caplog, "history_manager")

    # ── 调用点 4：profile_manager.save_profile ──
    def test_profile_save_refuses_write(self, tmp_path, monkeypatch, caplog):
        pp = tmp_path / "profiles.json"
        monkeypatch.setattr(profile_manager, "_profile_path", lambda: str(pp))
        monkeypatch.setattr(profile_manager, "_profiles_cache", None)
        monkeypatch.setattr(profile_manager, "_profiles_cache_time", 0)
        profile_manager.save_profile("1.2.3.4", {"res": 300})
        before = _read(pp)

        proc = _hold_lock(str(pp))
        try:
            with caplog.at_level(logging.ERROR):
                result = profile_manager.save_profile("5.6.7.8", {"res": 600})
        finally:
            self._release(proc)

        assert result is False
        assert _read(pp) == before, "原 profiles.json 必须原样保留"
        assert "5.6.7.8" not in json.loads(_read(pp)), "没持锁就不能写进去"
        assert _errors(caplog, "profile_manager")

    # ── 调用点 5：profile_manager.delete_profile ──
    def test_profile_delete_refuses_write(self, tmp_path, monkeypatch, caplog):
        pp = tmp_path / "profiles.json"
        monkeypatch.setattr(profile_manager, "_profile_path", lambda: str(pp))
        monkeypatch.setattr(profile_manager, "_profiles_cache", None)
        monkeypatch.setattr(profile_manager, "_profiles_cache_time", 0)
        profile_manager.save_profile("1.2.3.4", {"res": 300})
        before = _read(pp)

        proc = _hold_lock(str(pp))
        try:
            with caplog.at_level(logging.ERROR):
                result = profile_manager.delete_profile("1.2.3.4")
        finally:
            self._release(proc)

        assert result is False
        assert _read(pp) == before, "拿不到锁时不能删除"
        assert "1.2.3.4" in json.loads(_read(pp))
        assert _errors(caplog, "profile_manager")

    # ── 调用点 6：profile_manager.clear_all ──
    def test_profile_clear_all_refuses_write(self, tmp_path, monkeypatch, caplog):
        pp = tmp_path / "profiles.json"
        monkeypatch.setattr(profile_manager, "_profile_path", lambda: str(pp))
        monkeypatch.setattr(profile_manager, "_profiles_cache", None)
        monkeypatch.setattr(profile_manager, "_profiles_cache_time", 0)
        profile_manager.save_profile("1.2.3.4", {"res": 300})
        before = _read(pp)

        proc = _hold_lock(str(pp))
        try:
            with caplog.at_level(logging.ERROR):
                result = profile_manager.clear_all()
        finally:
            self._release(proc)

        assert result is False
        assert _read(pp) == before, "拿不到锁时不能清空"
        assert _errors(caplog, "profile_manager")

    # ── 调用点 7：scan_coordinator.save_config ──
    def test_scan_coordinator_save_config_refuses_write(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(scan_coordinator, "_app_dir", lambda: str(tmp_path))
        cfg = tmp_path / "scan_config.json"
        scan_coordinator.save_config({"old": True})
        before = _read(cfg)

        proc = _hold_lock(str(cfg))
        try:
            with caplog.at_level(logging.ERROR):
                result = scan_coordinator.save_config({"new": True})
        finally:
            self._release(proc)

        assert result is False
        assert _read(cfg) == before, "原 scan_config.json 必须原样保留"
        assert _errors(caplog, "scan_coordinator"), "必须记 error 日志（不能只记 debug）"


# ── 1. 两个真实子进程并发写：合法 JSON + 不丢更新 ─────────────


class TestConcurrentProcesses:
    """并发写必须由**真实子进程**验证（msvcrt 锁是进程级的）"""

    def test_two_processes_cache_config_final_is_one_complete_result(self, tmp_path):
        appdata = _appdata_dir(tmp_path)
        rounds, size = 5, 20000
        for tag, rc, so, se in _run_writers(_CACHE_WRITER, appdata, ["A", "B"], rounds, size):
            assert rc == 0, f"写进程 {tag} 失败: {se}"

        with open(os.path.join(_cfg_dir(appdata), "cache_config.json"), "r", encoding="utf-8") as f:
            data = json.load(f)  # 必须始终是合法 JSON
        assert data["tag"] in ("A", "B")
        assert data["blob"] == data["tag"] * size, "内容必须来自同一个写者（不能交错）"

    def test_two_processes_history_append_no_lost_update(self, tmp_path):
        appdata = _appdata_dir(tmp_path)
        rounds = 20
        for tag, rc, so, se in _run_writers(_HISTORY_WRITER, appdata, ["A", "B"], rounds):
            assert rc == 0, f"写进程 {tag} 失败: {se}"

        with open(os.path.join(_cfg_dir(appdata), "history.json"), "r", encoding="utf-8") as f:
            records = json.load(f)
        assert len(records) == 2 * rounds, "读-改-写全程持锁 ⇒ 一条都不能丢"
        assert {(r["tag"], r["seq"]) for r in records} == {
            (t, i) for t in ("A", "B") for i in range(rounds)}

    def test_two_processes_profiles_no_lost_update(self, tmp_path):
        appdata = _appdata_dir(tmp_path)
        rounds = 20
        for tag, rc, so, se in _run_writers(_PROFILE_WRITER, appdata, ["A", "B"], rounds):
            assert rc == 0, f"写进程 {tag} 失败: {se}"

        with open(os.path.join(_cfg_dir(appdata), "profiles.json"), "r", encoding="utf-8") as f:
            profiles = json.load(f)
        assert len(profiles) == 2 * rounds
        assert set(profiles) == {f"{t}-{i}" for t in ("A", "B") for i in range(rounds)}

    def test_two_processes_scan_config_always_valid_json(self, tmp_path):
        """scan_config.json 是整文件覆盖 ⇒ 判据是"始终合法 JSON、内容来自某一个写者" """
        appdata = _appdata_dir(tmp_path)
        rounds, size = 5, 20000
        for tag, rc, so, se in _run_writers(_SCAN_CONFIG_WRITER, appdata, ["A", "B"], rounds, size):
            assert rc == 0, f"写进程 {tag} 失败: {se}"

        with open(os.path.join(_cfg_dir(appdata), "scan_config.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["tag"] in ("A", "B")
        assert data["blob"] == data["tag"] * size, "内容必须来自同一个写者（不能交错）"


# ── 4. history 读失败保护（F-7 护栏没被改坏）─────────────────


class TestHistoryReadFailure:
    """读失败 ≠ 历史为空：绝不能用空列表覆盖原历史"""

    def test_read_oserror_does_not_overwrite_history(self, tmp_path, monkeypatch, caplog):
        hp = tmp_path / "history.json"
        monkeypatch.setattr(history_manager, "_history_file", lambda: str(hp))
        hp.write_text('[{"device_ip": "1.2.3.4"}]', encoding="utf-8")
        before = _read(hp)

        def boom(*args, **kwargs):
            raise OSError("文件被另一个进程独占")

        monkeypatch.setattr(history_manager, "open", boom, raising=False)
        with caplog.at_level(logging.ERROR):
            result = history_manager.append({"device_ip": "5.6.7.8"})

        assert result is False, "读失败必须放弃本次追加（返回 False），不能当成功"
        assert _read(hp) == before, "读失败不能覆盖原历史"
        assert _errors(caplog, "history_manager")

    def test_read_oserror_still_backs_up_original(self, tmp_path, monkeypatch):
        hp = tmp_path / "history.json"
        monkeypatch.setattr(history_manager, "_history_file", lambda: str(hp))
        original = '[{"device_ip": "1.2.3.4"}]'
        hp.write_text(original, encoding="utf-8")

        def boom(*args, **kwargs):
            raise OSError("文件被另一个进程独占")

        monkeypatch.setattr(history_manager, "open", boom, raising=False)
        history_manager.append({"device_ip": "5.6.7.8"})

        backups = [f for f in os.listdir(tmp_path) if ".corrupt-" in f]
        assert backups, "读失败也要尽力备份原文"
        with open(os.path.join(str(tmp_path), backups[0]), "r", encoding="utf-8") as f:
            assert f.read() == original

    def test_lock_timeout_does_not_leave_empty_history(self, tmp_path, monkeypatch):
        """★ 读路径的核心回归：锁拿不到 ⇒ 读根本没发生 ⇒ 绝不落成空历史"""
        hp = tmp_path / "history.json"
        monkeypatch.setattr(history_manager, "_history_file", lambda: str(hp))
        history_manager.append({"device_ip": "1.2.3.4"})
        monkeypatch.setattr(config_io, "_LOCK_WAIT_SECONDS", _SHORT_WAIT)

        proc = _hold_lock(str(hp))
        try:
            assert history_manager.append({"device_ip": "5.6.7.8"}) is False
        finally:
            proc.kill()
            proc.wait(timeout=30)

        records = json.loads(_read(hp))
        assert len(records) == 1, "历史不能被清空/覆盖成一条"
        assert records[0]["device_ip"] == "1.2.3.4"


# ── 5 / 6. 正常路径没被改坏 ─────────────────────────────────


class TestNormalPaths:
    def test_history_append_grows_count(self, tmp_path, monkeypatch):
        hp = tmp_path / "history.json"
        monkeypatch.setattr(history_manager, "_history_file", lambda: str(hp))
        for i in range(3):
            assert history_manager.append({"idx": i}) is True
        assert len(json.loads(_read(hp))) == 3
        assert len(history_manager.list_records(limit=999)) == 3

    def test_history_clear_normal_path(self, tmp_path, monkeypatch):
        hp = tmp_path / "history.json"
        monkeypatch.setattr(history_manager, "_history_file", lambda: str(hp))
        history_manager.append({"idx": 1})
        assert history_manager.clear() is True
        assert json.loads(_read(hp)) == []

    def test_cache_manager_save_cache_config_normal_path(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cache_config.json"
        monkeypatch.setattr(cache_manager, "_cache_config_path", lambda: str(cfg))
        monkeypatch.setattr(cache_manager, "_config_cache", None)
        monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
        assert cache_manager.save_cache_config({"cache_limit_mb": 512}) is True
        assert json.loads(_read(cfg)) == {"cache_limit_mb": 512}

    def test_profile_save_delete_clear_all_normal_path(self, tmp_path, monkeypatch):
        pp = tmp_path / "profiles.json"
        monkeypatch.setattr(profile_manager, "_profile_path", lambda: str(pp))
        monkeypatch.setattr(profile_manager, "_profiles_cache", None)
        monkeypatch.setattr(profile_manager, "_profiles_cache_time", 0)

        assert profile_manager.save_profile("1.2.3.4", {"res": 300}) is True
        assert profile_manager.get_profile("1.2.3.4") == {"res": 300}

        assert profile_manager.delete_profile("1.2.3.4") is True
        assert profile_manager.get_profile("1.2.3.4") == {}

        profile_manager.save_profile("5.6.7.8", {"res": 600})
        assert profile_manager.clear_all() is True
        assert profile_manager.get_all_profiles() == {}

    def test_scan_coordinator_save_config_normal_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scan_coordinator, "_app_dir", lambda: str(tmp_path))
        assert scan_coordinator.save_config({"resolution": 600}) is True
        assert json.loads(_read(tmp_path / "scan_config.json")) == {"resolution": 600}


# ── 7. 进程内 _config_lock 仍在 ─────────────────────────────


class _TrackingLock:
    """记录进入次数的线程锁（用来证明 _config_lock 仍在事务路径上）"""

    def __init__(self):
        self._lock = threading.Lock()
        self.entered = 0

    def __enter__(self):
        self._lock.acquire()
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


class TestInProcessLock:
    def test_in_process_config_lock_still_in_path(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cache_config.json"
        monkeypatch.setattr(cache_manager, "_cache_config_path", lambda: str(cfg))
        monkeypatch.setattr(cache_manager, "_config_cache", None)
        monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
        tracker = _TrackingLock()
        monkeypatch.setattr(cache_manager, "_config_lock", tracker)

        cache_manager.save_cache_config({"a": 1})
        assert tracker.entered == 1, "进程内 _config_lock 不能因为换了跨进程锁就被删掉"

    def test_in_process_concurrent_writes_do_not_lose_updates(self, tmp_path, monkeypatch):
        """同进程多线程并发写：不能抛异常，产出必须是合法 JSON"""
        cfg = tmp_path / "cache_config.json"
        monkeypatch.setattr(cache_manager, "_cache_config_path", lambda: str(cfg))
        monkeypatch.setattr(cache_manager, "_config_cache", None)
        monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
        errors = []

        def writer(tag):
            try:
                for i in range(20):
                    cache_manager.save_cache_config({"tag": tag, "seq": i})
            except Exception as e:  # pragma: no cover - 失败即视为缺陷
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        data = json.loads(_read(cfg))
        assert data["tag"] in ("A", "B")
