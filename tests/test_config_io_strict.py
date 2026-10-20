"""
config_io 严格锁回归测试 — HP Scan Tool v4.0

锁死两条 finding：
  I-1（Important）：跨进程锁等待超时后**仍写入** → 丢更新
       - 严格锁 config_write_lock / config_lock(required=True)：拿不到锁就抛
         ConfigLockTimeout，事务体不执行
       - 默认 config_lock(required=False) 的旧行为**保持不变**
       - 默认等待上限从 2 秒提到 30 秒：覆盖真实持锁耗时（读 → sleep → 写）
  F-1（Important）：锁文件**首次创建**时的初始化写竞争
       - 两个进程同时看到 size==0 并各写一次字节 0，后写者撞上 Windows
         字节区间锁 → PermissionError 逃逸 → 整条写事务被放弃
       - 修法：初始化写容错 + 拿到锁后补写，PermissionError 绝不逃逸

跨进程用子进程测（msvcrt 的锁是"文件句柄 × 字节区间"级别的，
同进程多线程也能冲突，但子进程更接近真实场景）。
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

import config_io
from config_io import ConfigLockTimeout

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程写脚本：在同一个"读 → sleep → 写"事务里加自己的 key
#   argv: path, tag, hold_seconds, start_at, project_root, strict(0/1)
_LOCK_WRITER = r'''
import json, os, sys, time
path, tag, hold, start_at, project_root, strict = sys.argv[1:7]
sys.path.insert(0, project_root)
import config_io
while time.time() < float(start_at):
    time.sleep(0.005)
if strict == "1":
    ctx = config_io.config_write_lock(path)
else:
    ctx = config_io.config_lock(path)
with ctx:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    time.sleep(float(hold))          # 持锁期间的"慢操作"
    data[tag] = True
    config_io.atomic_write_json(path, data)
'''


def _run_writer(path, tag, hold, start_at, strict):
    return subprocess.Popen(
        [sys.executable, "-c", _LOCK_WRITER, path, tag, str(hold),
         str(start_at), PROJECT_ROOT, "1" if strict else "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


# ── I-1：跨进程不再丢更新 ──────────────────────────────────


class TestNoLostUpdateAcrossProcesses:
    """复核方复现：A 持锁做"读 → sleep 3s → 写"，B 在 0.3 秒后进入做同样的事。
    旧实现里 B 等不到锁（2 秒上限）却照样写 ⇒ B 的更新被 A 整条覆盖。"""

    def _run_pair(self, tmp_path, strict):
        target = str(tmp_path / "cfg.json")
        hold = 3.0
        start_at = time.time() + 0.6
        procs = [_run_writer(target, tag, hold, start_at, strict)
                 for tag in ("A", "B")]
        for p in procs:
            so, se = p.communicate(timeout=120)
            assert p.returncode == 0, se.decode("utf-8", "replace")
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_strict_lock_keeps_both_keys(self, tmp_path):
        """严格锁：两个进程的 key 必须都在（B 的写入不会把 A 的覆盖掉）"""
        data = self._run_pair(tmp_path, strict=True)
        assert data.get("A") is True, "A 的更新丢了"
        assert data.get("B") is True, "B 的更新丢了"

    def test_default_lock_waits_long_enough(self, tmp_path):
        """默认 config_lock：等待上限 30 秒足以覆盖持锁方的真实耗时
        （复核方实测持锁方要 3 秒），所以也不会丢更新。"""
        data = self._run_pair(tmp_path, strict=False)
        assert data.get("A") is True, "A 的更新丢了"
        assert data.get("B") is True, "B 的更新丢了"

    def test_default_lock_wait_is_at_least_30_seconds(self):
        """默认等待上限必须是 30 秒（2 秒在机器满载/fsync 慢时会被正常触发）"""
        assert config_io._LOCK_WAIT_SECONDS >= 30.0


# ── I-1：严格锁语义 ────────────────────────────────────────


class TestStrictLockSemantics:
    def _hold_lock(self, target):
        """占住锁（另一个句柄），返回上下文管理器"""
        return config_io.config_lock(target)

    def test_required_true_raises_and_body_does_not_run(self, tmp_path):
        """required=True：拿不到锁抛 ConfigLockTimeout，事务体**不执行**"""
        target = str(tmp_path / "cfg.json")
        ran = []
        with self._hold_lock(target):
            with pytest.raises(ConfigLockTimeout):
                with config_io.config_lock(target, timeout=0.1, required=True):
                    ran.append("body")
        assert ran == [], "拿不到锁时事务体绝不能执行（否则就是未持锁还写入）"

    def test_required_true_does_not_write_the_file(self, tmp_path):
        """required=True：事务体没跑 ⇒ 文件没被写"""
        target = str(tmp_path / "cfg.json")
        with self._hold_lock(target):
            with pytest.raises(ConfigLockTimeout):
                with config_io.config_lock(target, timeout=0.1, required=True):
                    config_io.atomic_write_json(target, {"written": True})
        assert not os.path.exists(target), "未持锁时不允许写入"

    def test_config_write_lock_raises_and_body_does_not_run(self, tmp_path):
        """严格版 helper config_write_lock 语义与 required=True 一致"""
        target = str(tmp_path / "cfg.json")
        ran = []
        with self._hold_lock(target):
            with pytest.raises(ConfigLockTimeout):
                with config_io.config_write_lock(target, timeout=0.1):
                    ran.append("body")
        assert ran == []

    def test_required_false_keeps_old_behavior(self, tmp_path):
        """默认（required=False）：仍是旧行为 —— yield False，不抛异常"""
        target = str(tmp_path / "cfg.json")
        with self._hold_lock(target):
            with config_io.config_lock(target, timeout=0.1) as acquired:
                assert acquired is False, "拿不到锁时必须 yield False（老调用点靠这个）"

    def test_required_false_body_still_runs(self, tmp_path):
        """旧行为：默认模式下事务体照常执行（不改变既有调用点的语义）"""
        target = str(tmp_path / "cfg.json")
        ran = []
        with self._hold_lock(target):
            with config_io.config_lock(target, timeout=0.1):
                ran.append("body")
        assert ran == ["body"]

    def test_lock_is_acquired_when_free(self, tmp_path):
        """没人占锁时，严格锁与默认锁都能正常拿到"""
        target = str(tmp_path / "cfg.json")
        with config_io.config_write_lock(target) as acquired:
            assert acquired is True
        with config_io.config_lock(target) as acquired:
            assert acquired is True


# ── F-1：锁文件初始化写竞争 ────────────────────────────────


class TestLockFileInitRace:
    """F-1：.lock 文件首次创建时，两个进程可能同时看到 size==0 并各写一次字节 0。
    Windows 的字节区间锁对**写操作**是强制的 ⇒ 后写者 PermissionError
    （异常逃逸 → 整条写事务被放弃 → 扫描历史/设备配置/页元数据静默丢失）。"""

    def test_init_write_does_not_raise_when_byte_already_locked(self, tmp_path):
        """确定性复现：另一个句柄已锁住字节 0，config_lock 不得抛 PermissionError"""
        import msvcrt

        target = str(tmp_path / "cfg.json")
        lock_path = target + ".lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)   # 0 长度 ⇒ 会走初始化写分支
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            # Windows 允许锁定超出 EOF 的字节区间
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            with config_io.config_lock(target, timeout=0.2) as acquired:
                assert acquired is False, "锁被别人占着，应当拿不到"
        finally:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)

    def test_init_write_does_not_raise_in_strict_mode(self, tmp_path):
        """严格锁同样不得因初始化写竞争而抛 PermissionError"""
        import msvcrt

        target = str(tmp_path / "cfg.json")
        lock_path = target + ".lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            with pytest.raises(ConfigLockTimeout):
                with config_io.config_write_lock(target, timeout=0.2):
                    pass
        finally:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)

    def test_many_processes_creating_lock_file_together(self, tmp_path):
        """压力版：多个进程同时首次创建同一个 .lock 文件，谁都不许因初始化写而炸"""
        target = str(tmp_path / "cfg.json")
        start_at = time.time() + 0.4
        procs = [_run_writer(target, tag, 0.2, start_at, True)
                 for tag in ("A", "B", "C", "D")]
        for p in procs:
            so, se = p.communicate(timeout=120)
            assert p.returncode == 0, (
                f"子进程异常退出（多半是 PermissionError）：{se.decode('utf-8', 'replace')}")
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert set(data) == {"A", "B", "C", "D"}, "并发写丢了更新"

    def test_threads_creating_lock_file_together(self, tmp_path):
        """同进程多线程首次创建同一个 .lock 文件：不得抛 PermissionError"""
        target = str(tmp_path / "cfg.json")
        errors = []

        def worker():
            try:
                with config_io.config_write_lock(target):
                    pass
            except Exception as e:      # noqa: BLE001 - 记录任何逃逸的异常
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == [], f"初始化写竞争导致异常逃逸: {errors}"
