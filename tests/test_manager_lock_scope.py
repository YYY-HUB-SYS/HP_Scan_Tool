"""
管理器锁作用域护栏 — HP Scan Tool v4.0（终审 Important I-2）

被守护的行为
------------
**等待跨进程锁期间不得持有进程内锁。**

这些管理器（history_manager / profile_manager）的结构原本是
「进程内 `threading.Lock` ⇒ 再进跨进程锁」：

    with _lock:                              # 进程内锁
        with config_io.config_write_lock(p): # 跨进程锁，最长等 30 秒

⇒ 若**第二个进程**正持有那把跨进程锁，本进程的后台线程先抢到 `_lock`
   再去等跨进程锁 ⇒ 它**攥着进程内锁等 30 秒**
⇒ 同进程其它线程（哪怕只是**读一次历史** / 读一次设备档案）被连带堵住
⇒ GUI 线程假死最长 30 秒（界面"未响应"）。

修复后的结构：**跨进程锁在外、进程内锁在内**，
进程内锁只保护"内存状态 + 事务体"那一小段（不含跨进程锁的等待）。

★ 一致性保证（读-改-写不丢更新）由**进程内锁仍然覆盖整个事务体**来保证：
  事务体（读盘 → 改 → 写盘）整体在 `_lock` 内 ⇒ 同进程内不可能有两个线程
  同时做读-改-写 ⇒ 既有护栏（8 线程 × 25 次 ⇒ 200/200 不丢不重）不受影响。
  跨进程那一侧仍由 `config_write_lock` 保证（两个进程各持一把文件锁）。
"""

import ast
import contextlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest

import config_io
import history_manager
import profile_manager


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程脚本：**真实地**持有跨进程锁不放（模拟"另一个实例正持有那把锁"）
_HOLD_LOCK = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
import config_io
with config_io.config_write_lock(sys.argv[2]):
    print("HOLDING", flush=True)
    time.sleep(float(sys.argv[3]))
'''


# 跨进程锁"等待"时长（模拟另一个进程正持有那把锁）
_WAIT_SECONDS = 2.0
# 读路径必须在它之内返回（留足 CI 抖动余量）
_READ_BUDGET = 0.8


@contextlib.contextmanager
def _slow_cross_process_lock(monkeypatch, entered):
    """把 `config_io.config_write_lock` 换成"先卡 2 秒再拿真锁"的版本。

    ★ 模拟的是**等待跨进程锁**的那一段（另一个进程正持有那把锁），
      不是"持有锁不放"——后者会触发 ConfigLockTimeout，走的是另一条分支。
    """
    real = config_io.config_write_lock

    @contextlib.contextmanager
    def slow(path, timeout=None):
        entered.set()
        time.sleep(_WAIT_SECONDS)
        with real(path, timeout=timeout):
            yield True

    monkeypatch.setattr(config_io, "config_write_lock", slow)


def _run_writer(fn, entered):
    """后台线程：跑一次写操作（会卡在跨进程锁上）"""
    errors = []

    def target():
        try:
            fn()
        except Exception as exc:            # pragma: no cover - 失败即缺陷
            errors.append(exc)

    th = threading.Thread(target=target)
    th.start()
    assert entered.wait(timeout=10), "写线程没进入跨进程锁等待"
    return th, errors


# ── 读路径不得被写路径的"锁等待"连带堵住 ─────────────────────


class TestReadersNotBlockedByLockWait:
    def test_history_read_not_blocked_by_writer_waiting_for_cross_process_lock(
            self, tmp_config_dir, monkeypatch):
        """★ GUI 线程读历史（打开历史面板）不得被写线程的锁等待堵住。

        终审 F-2 实测：第二个进程持锁 ⇒ GUI 线程最长假死 30 秒。
        """
        entered = threading.Event()
        _slow_cross_process_lock(monkeypatch, entered)

        th, errors = _run_writer(
            lambda: history_manager.append({"device_ip": "1.2.3.4"}), entered)
        try:
            # ① 结构判据：等待跨进程锁期间**不得**持有进程内锁
            assert not history_manager._lock.locked(), (
                "等待跨进程锁期间进程内锁仍被持有 ⇒ 同进程其它线程被连带堵住")

            # ② 行为判据：读一次历史必须立刻返回
            t0 = time.monotonic()
            history_manager.list_records()
            elapsed = time.monotonic() - t0
            assert elapsed < _READ_BUDGET, (
                "读历史被写线程的跨进程锁等待堵住了 %.2f 秒（上限 %.2f 秒）"
                % (elapsed, _READ_BUDGET))
        finally:
            th.join(timeout=30)
        assert errors == []

    def test_profile_read_not_blocked_by_writer_waiting_for_cross_process_lock(
            self, tmp_path, monkeypatch):
        """★ GUI 线程读设备档案（切设备时读档案）不得被写线程的锁等待堵住。"""
        appdata = tmp_path / "appdata"
        cfg = appdata / "HP_Scan_Tool"
        cfg.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setattr(profile_manager, "_app_dir", str(cfg), raising=False)
        monkeypatch.setattr(profile_manager, "_profiles_cache", None, raising=False)
        monkeypatch.setattr(profile_manager, "_profiles_cache_time", 0, raising=False)

        entered = threading.Event()
        _slow_cross_process_lock(monkeypatch, entered)

        th, errors = _run_writer(
            lambda: profile_manager.save_profile("1.2.3.4", {"res": 300}), entered)
        try:
            assert not profile_manager._lock.locked(), (
                "等待跨进程锁期间进程内锁仍被持有 ⇒ 同进程其它线程被连带堵住")

            t0 = time.monotonic()
            profile_manager.get_profile("1.2.3.4")
            profile_manager.get_all_profiles()
            elapsed = time.monotonic() - t0
            assert elapsed < _READ_BUDGET, (
                "读设备档案被写线程的跨进程锁等待堵住了 %.2f 秒（上限 %.2f 秒）"
                % (elapsed, _READ_BUDGET))
        finally:
            th.join(timeout=30)
        assert errors == []

    def test_history_read_not_blocked_while_other_process_holds_the_lock(
            self, tmp_config_dir, monkeypatch):
        """★ 最贴近终审场景的复现：**另一个真实进程**正持有那把跨进程锁。

        终审 F-2 的触发条件原文：「同机上**第二个实例**（本项目 GUI 与 CLI
        都是独立进程，共用 %APPDATA%\\HP_Scan_Tool\\）正持有同一把跨进程锁
        且迟迟不释放」。

        ⇒ 本进程的后台线程去等那把锁（最长 30 秒）时，
          **GUI 线程**（读一次历史）必须立刻返回。
        """
        # 缩短等待上限：让"被堵住"这件事在几秒内显现（不改变被守护的性质）
        monkeypatch.setattr(config_io, "_LOCK_WAIT_SECONDS", 3.0)
        hp = history_manager._history_file()

        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLD_LOCK, PROJECT_ROOT, hp, "30"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        th = None
        try:
            line = proc.stdout.readline().strip()
            assert line == b"HOLDING", "子进程没拿到跨进程锁: %r" % line

            errors = []

            def writer():
                try:
                    # 这一句会**卡在跨进程锁上**（另一个进程正持有它）
                    history_manager.append({"device_ip": "1.2.3.4"})
                except Exception as exc:        # pragma: no cover - 失败即缺陷
                    errors.append(exc)

            th = threading.Thread(target=writer)
            th.start()
            time.sleep(0.5)
            assert th.is_alive(), "写线程应该正卡在跨进程锁上（没卡住就没测到东西）"

            t0 = time.monotonic()
            history_manager.list_records()
            elapsed = time.monotonic() - t0
            assert elapsed < _READ_BUDGET, (
                "另一个进程持有跨进程锁时，读历史被堵了 %.2f 秒（上限 %.2f 秒）"
                "⇒ GUI 线程会假死" % (elapsed, _READ_BUDGET))
            assert errors == []
        finally:
            proc.kill()
            proc.wait(timeout=30)
            if th is not None:
                th.join(timeout=60)


# ── 既有并发护栏（8 线程 × 25 次 ⇒ 200/200 不丢不重）────────────


class TestConcurrencyGuaranteesStillHold:
    """★ 锁作用域调整**不得**破坏读-改-写的一致性。"""

    def test_history_concurrent_appends_no_lost_update(self, tmp_config_dir):
        """8 线程 × 25 次 ⇒ 200 条，不丢不重。"""
        errors = []

        def writer(tag):
            try:
                for i in range(25):
                    if not history_manager.append({"t": tag, "i": i}):
                        errors.append((tag, i))
            except Exception as exc:        # pragma: no cover - 失败即缺陷
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)

        assert errors == [], "并发写出现失败：%r" % (errors[:5],)
        records = history_manager.list_records(limit=9999)
        assert len(records) == 200, "记录数应为 200，实际 %d" % len(records)
        assert len({(r["t"], r["i"]) for r in records}) == 200, "有记录丢了或重了"

    def test_profile_concurrent_saves_no_lost_update(self, tmp_path, monkeypatch):
        """8 线程 × 25 次 ⇒ 200 个档案，不丢不重。"""
        appdata = tmp_path / "appdata"
        cfg = appdata / "HP_Scan_Tool"
        cfg.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setattr(profile_manager, "_app_dir", str(cfg), raising=False)
        monkeypatch.setattr(profile_manager, "_profiles_cache", None, raising=False)
        monkeypatch.setattr(profile_manager, "_profiles_cache_time", 0, raising=False)

        errors = []

        def writer(tag):
            try:
                for i in range(25):
                    if not profile_manager.save_profile("%s-%d" % (tag, i),
                                                       {"t": tag, "i": i}):
                        errors.append((tag, i))
            except Exception as exc:        # pragma: no cover - 失败即缺陷
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)

        assert errors == [], "并发写出现失败：%r" % (errors[:5],)
        profiles = profile_manager.get_all_profiles()
        assert len(profiles) == 200, "档案数应为 200，实际 %d" % len(profiles)
        assert len({(p["t"], p["i"]) for p in profiles.values()}) == 200, (
            "有档案丢了或重了")

        # 文件本身也必须是合法 JSON 且条数一致
        path = os.path.join(str(cfg), "profiles.json")
        with open(path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert len(on_disk) == 200


# ── 锁的**顺序**必须一致（跨进程锁在外 ⇒ 不会死锁）──────────────


class TestLockOrderIsUniform:
    """所有路径统一「跨进程锁在外、进程内锁在内」⇒ 不存在锁序反转。"""

    @staticmethod
    def _dotted(node):
        """把 AST 里的调用还原成点号路径（config_io.config_write_lock / _lock）"""
        if isinstance(node, ast.Attribute):
            return "%s.%s" % (TestLockOrderIsUniform._dotted(node.value),
                              node.attr)
        if isinstance(node, ast.Name):
            return node.id
        return ""

    @staticmethod
    def _with_items(node):
        """一个 `with` 语句里管理的所有资源名"""
        names = []
        for item in node.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call):
                ctx = ctx.func
            names.append(TestLockOrderIsUniform._dotted(ctx))
        return names

    @classmethod
    def _scan(cls, path):
        """返回 (违规 A 列表, 违规 B 列表)"""
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)

        bad_order = []      # 进程内锁**包住**了跨进程锁（横跨锁等待）
        missing = []        # 跨进程锁之内没有进程内锁（事务体没被保护）
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            names = cls._with_items(node)
            if "_lock" in names:
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.With)
                            and sub is not node
                            and "config_io.config_write_lock" in cls._with_items(sub)):
                        bad_order.append((path, sub.lineno))
            if "config_io.config_write_lock" in names:
                inner = []
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.With)
                            and sub is not node
                            and "_lock" in cls._with_items(sub)):
                        inner.append(sub)
                if not inner:
                    missing.append((path, node.lineno))
        return bad_order, missing

    @pytest.mark.parametrize("name", ["history_manager.py", "profile_manager.py"])
    def test_process_lock_never_spans_the_cross_process_lock_wait(self, name):
        """★ 违规 A：进程内锁**不得**包住跨进程锁。

        那正是终审 F-2 的根因：进程内锁横跨最长 30 秒的跨进程锁等待
        ⇒ 同进程其它线程（GUI 线程）被连带堵住。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bad_order, _missing = self._scan(os.path.join(root, name))
        assert bad_order == [], (
            "进程内锁横跨了跨进程锁的等待：%r" % (bad_order,))

    @pytest.mark.parametrize("name", ["history_manager.py", "profile_manager.py"])
    def test_read_modify_write_body_stays_under_the_process_lock(self, name):
        """★ 违规 B：跨进程锁之内**必须**还有进程内锁。

        事务体（读盘 → 改 → 写盘）整体在进程内锁内 ⇒ 同进程内不可能有两个
        线程同时做读-改-写 ⇒ 既有护栏（8 线程 × 25 次 ⇒ 200/200）不受影响。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _bad_order, missing = self._scan(os.path.join(root, name))
        assert missing == [], (
            "跨进程锁之内没有进程内锁 ⇒ 读-改-写事务体失去进程内保护：%r"
            % (missing,))
