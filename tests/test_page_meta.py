"""
page_meta 单元测试 — HP Scan Tool v4.0
覆盖: 按 job 存取 / job 隔离 / 原子写 / 损坏文件容错
      + 并发写不丢更新（线程 + 进程）/ 孤儿清理
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

import config_io
from page_meta import PageMeta, PageMetaStore, prune_orphans

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程写脚本：两个进程同时写同一份 page_meta.json（走真实代码路径）
# start_at 是统一的起跑时刻（墙钟），让多个子进程的写事务真正重叠
_META_WRITER = r'''
import os, sys, time
project_root, cache_root, tag, rounds, start_at = sys.argv[1:6]
sys.path.insert(0, project_root)
from page_meta import PageMeta, PageMetaStore
store = PageMetaStore(cache_root)
while time.time() < float(start_at):
    time.sleep(0.005)
for i in range(int(rounds)):
    store.set_page("job_" + tag, i, PageMeta(cache_path="%s-%d.jpg" % (tag, i), width=1, height=2))
'''


@pytest.fixture
def cache_root(tmp_path):
    """缓存根目录：page_meta.json 与各 job 目录都在它下面"""
    root = str(tmp_path / "cache")
    os.makedirs(root, exist_ok=True)
    return root


@pytest.fixture
def store(cache_root):
    return PageMetaStore(cache_root)


def _make_job_dir(cache_root, job_id):
    """建缓存 job 目录（元数据条目"不是孤儿"的前提：真实流程里
    cache_manager.write_page 先建目录，随后才记录页元数据）"""
    d = os.path.join(cache_root, job_id)
    os.makedirs(d, exist_ok=True)
    return d


class TestPageMetaStore:
    def test_set_and_get_page(self, store):
        store.set_page("job1", 1, PageMeta(
            cache_path="C:/cache/job1/page_001.jpg", width=2480, height=3507))
        job = store.get_job("job1")
        assert "1" in job
        assert job["1"]["cache_path"] == "C:/cache/job1/page_001.jpg"
        assert job["1"]["width"] == 2480
        assert job["1"]["height"] == 3507

    def test_defaults_filled(self, store):
        store.set_page("job1", 1, PageMeta(cache_path="p.jpg", width=10, height=20))
        meta = store.get_job("job1")["1"]
        assert meta["manual_rotation"] == 0
        assert meta["deskew_angle"] == 0.0
        assert meta["a4_normalized"] is False
        assert meta["crop_rect"] is None

    def test_job_isolation(self, store):
        store.set_page("jobA", 1, PageMeta(cache_path="a.jpg", width=1, height=2))
        store.set_page("jobB", 1, PageMeta(cache_path="b.jpg", width=3, height=4))
        assert store.get_job("jobA")["1"]["cache_path"] == "a.jpg"
        assert store.get_job("jobB")["1"]["cache_path"] == "b.jpg"
        assert set(store.load().keys()) == {"jobA", "jobB"}

    def test_remove_job_only_removes_that_job(self, store, cache_root):
        # 没有缓存 job 目录的条目按定义是孤儿，remove_job 会顺手清掉，
        # 所以这里给两个 job 都建目录（与真实流程一致）
        _make_job_dir(cache_root, "jobA")
        _make_job_dir(cache_root, "jobB")
        store.set_page("jobA", 1, PageMeta(cache_path="a.jpg", width=1, height=2))
        store.set_page("jobB", 1, PageMeta(cache_path="b.jpg", width=3, height=4))
        store.remove_job("jobA")
        assert store.get_job("jobA") == {}
        assert store.get_job("jobB")["1"]["cache_path"] == "b.jpg"

    def test_remove_missing_job_is_noop(self, store, cache_root):
        _make_job_dir(cache_root, "jobA")
        store.set_page("jobA", 1, PageMeta(cache_path="a.jpg", width=1, height=2))
        store.remove_job("does-not-exist")
        assert "jobA" in store.load()

    def test_file_lives_in_cache_root_not_job_dir(self, tmp_path):
        """元数据文件放缓存根目录（不被 eviction 当 job 目录处理）"""
        root = str(tmp_path / "cache")
        s = PageMetaStore(root)
        s.set_page("job1", 1, PageMeta(cache_path="p.jpg", width=1, height=2))
        assert os.path.exists(os.path.join(root, "page_meta.json"))
        assert not os.path.exists(os.path.join(root, "job1"))

    def test_load_missing_returns_empty(self, store):
        assert store.load() == {}
        assert store.get_job("job1") == {}

    def test_load_corrupt_returns_empty(self, store):
        os.makedirs(os.path.dirname(store._path), exist_ok=True)
        with open(store._path, "w", encoding="utf-8") as f:
            f.write("{not json")
        assert store.load() == {}
        assert store.get_job("job1") == {}

    def test_atomic_write_keeps_original_on_failure(self, store, monkeypatch):
        """写失败时原文件必须完好（原子写：唯一临时名 + os.replace）"""
        store.set_page("job1", 1, PageMeta(cache_path="good.jpg", width=1, height=2))
        before = store.load()

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(json, "dump", boom)
        with pytest.raises(RuntimeError):
            store.set_page("job1", 2, PageMeta(cache_path="bad.jpg", width=3, height=4))

        assert store.load() == before
        assert store.get_job("job1")["1"]["cache_path"] == "good.jpg"
        assert "2" not in store.get_job("job1")

    def test_atomic_write_leaves_no_tmp_and_keeps_original(self, store, monkeypatch):
        """替换失败时：旧文件原样保留，且不残留 .tmp"""
        store.set_page("job1", 1, PageMeta(cache_path="good.jpg", width=1, height=2))
        before = store.load()

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(config_io.os, "replace", boom)
        with pytest.raises(OSError):
            store.set_page("job1", 2, PageMeta(cache_path="bad.jpg", width=3, height=4))

        assert store.load() == before
        leftovers = [f for f in os.listdir(os.path.dirname(store._path))
                     if f.endswith(".tmp")]
        assert leftovers == []

    def test_unicode_path_roundtrip(self, store):
        store.set_page("job1", 1, PageMeta(
            cache_path="C:/扫描/第1页.jpg", width=1, height=2))
        assert store.get_job("job1")["1"]["cache_path"] == "C:/扫描/第1页.jpg"


# ── 并发写：不丢更新 ────────────────────────────────────────


class TestConcurrentSetPage:
    def test_two_threads_different_jobs_no_lost_update(self, store):
        """两个线程同时写不同 job：谁都不能被覆盖掉"""
        rounds = 20
        barrier = threading.Barrier(2)
        errors = []

        def writer(tag):
            try:
                barrier.wait(timeout=30)
                for i in range(rounds):
                    store.set_page(f"job_{tag}", i, PageMeta(
                        cache_path=f"{tag}-{i}.jpg", width=1, height=2))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert errors == []
        metas = store.load()
        assert set(metas) == {"job_A", "job_B"}, "有 job 被整段覆盖掉了"
        for tag in ("A", "B"):
            assert len(metas[f"job_{tag}"]) == rounds, f"job_{tag} 的页丢了"

    def test_two_threads_same_job_different_pages_no_lost_update(self, store):
        """两个线程同时写同一个 job 的不同页：一页都不能丢"""
        rounds = 20
        barrier = threading.Barrier(2)
        errors = []

        def writer(tag):
            try:
                barrier.wait(timeout=30)
                for i in range(rounds):
                    store.set_page("job_shared", i * 2 + (0 if tag == "A" else 1),
                                   PageMeta(cache_path=f"{tag}-{i}.jpg", width=1, height=2))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert errors == []
        metas = store.load()
        assert set(metas) == {"job_shared"}
        assert len(metas["job_shared"]) == rounds * 2, "同一 job 的页丢了"

    def test_slow_read_window_is_closed(self, store, monkeypatch):
        """
        把"读-改-写"的窗口人为拉大（读之后 sleep）：

        没有锁时两个线程必然交错 ⇒ 后写的把先写的整段覆盖掉；
        有锁时临界区串行 ⇒ 一条都不能丢。
        """
        real_load = PageMetaStore.load

        def slow_load(self):
            data = real_load(self)
            time.sleep(0.02)
            return data

        monkeypatch.setattr(PageMetaStore, "load", slow_load)

        rounds = 5
        barrier = threading.Barrier(2)
        errors = []

        def writer(tag):
            try:
                barrier.wait(timeout=30)
                for i in range(rounds):
                    store.set_page(f"job_{tag}", i, PageMeta(
                        cache_path=f"{tag}-{i}.jpg", width=1, height=2))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert errors == []
        metas = store.load()
        assert set(metas) == {"job_A", "job_B"}
        for tag in ("A", "B"):
            assert len(metas[f"job_{tag}"]) == rounds

    def test_two_processes_no_lost_update(self, cache_root):
        """两个进程同时写同一份 page_meta.json：一条都不能丢"""
        rounds = 20
        start_at = time.time() + 0.6   # 统一起跑时刻：让写事务真正重叠
        procs = []
        for tag in ("A", "B"):
            procs.append(subprocess.Popen(
                [sys.executable, "-c", _META_WRITER, PROJECT_ROOT, cache_root,
                 tag, str(rounds), str(start_at)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        for p in procs:
            so, se = p.communicate(timeout=180)
            assert p.returncode == 0, se.decode("utf-8", "replace")

        metas = PageMetaStore(cache_root).load()
        assert set(metas) == {"job_A", "job_B"}, "有 job 被整段覆盖掉了"
        for tag in ("A", "B"):
            assert len(metas[f"job_{tag}"]) == rounds, f"job_{tag} 的页丢了"

    def test_no_tmp_left_after_concurrent_writes(self, cache_root):
        """并发写结束后不能有残留 .tmp"""
        rounds = 10
        start_at = time.time() + 0.6
        procs = [subprocess.Popen(
            [sys.executable, "-c", _META_WRITER, PROJECT_ROOT, cache_root,
             tag, str(rounds), str(start_at)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) for tag in ("A", "B")]
        for p in procs:
            so, se = p.communicate(timeout=180)
            assert p.returncode == 0, se.decode("utf-8", "replace")

        leftovers = [f for f in os.listdir(cache_root) if f.endswith(".tmp")]
        assert leftovers == []


# ── 孤儿清理 ────────────────────────────────────────────────


class TestPruneOrphans:
    def test_orphan_removed_and_live_job_kept(self, store, cache_root):
        """元数据有、缓存目录没有 ⇒ 孤儿，删掉；缓存目录还在 ⇒ 必须保留"""
        _make_job_dir(cache_root, "alive")
        store.set_page("orphan", 1, PageMeta(cache_path="o.jpg", width=1, height=2))
        store.set_page("alive", 1, PageMeta(cache_path="a.jpg", width=1, height=2))

        removed = store.prune_orphans()

        assert removed == ["orphan"]
        metas = store.load()
        assert "orphan" not in metas
        assert metas["alive"]["1"]["cache_path"] == "a.jpg"

    def test_prune_is_idempotent(self, store, cache_root):
        _make_job_dir(cache_root, "alive")
        store.set_page("orphan", 1, PageMeta(cache_path="o.jpg", width=1, height=2))
        store.set_page("alive", 1, PageMeta(cache_path="a.jpg", width=1, height=2))

        assert store.prune_orphans() == ["orphan"]
        assert store.prune_orphans() == []
        assert set(store.load()) == {"alive"}

    def test_remove_job_also_prunes_orphans(self, store, cache_root):
        """remove_job 被调用时，顺手清掉其它孤儿条目"""
        _make_job_dir(cache_root, "alive")
        store.set_page("orphan", 1, PageMeta(cache_path="o.jpg", width=1, height=2))
        store.set_page("alive", 1, PageMeta(cache_path="a.jpg", width=1, height=2))

        store.remove_job("alive")

        metas = store.load()
        assert "orphan" not in metas
        assert "alive" not in metas

    def test_prune_keeps_meta_when_cache_root_missing(self, store, tmp_path):
        """缓存根不存在时不动元数据（避免把全部条目误当孤儿删光）"""
        store.set_page("job1", 1, PageMeta(cache_path="p.jpg", width=1, height=2))

        removed = store.prune_orphans(cache_root=str(tmp_path / "no_such_root"))

        assert removed == []
        assert "job1" in store.load()

    def test_module_level_prune_orphans(self, cache_root):
        """模块级入口：按缓存根清理孤儿"""
        _make_job_dir(cache_root, "alive")
        store = PageMetaStore(cache_root)
        store.set_page("orphan", 1, PageMeta(cache_path="o.jpg", width=1, height=2))
        store.set_page("alive", 1, PageMeta(cache_path="a.jpg", width=1, height=2))

        removed = prune_orphans(cache_root)

        assert removed == ["orphan"]
        assert set(store.load()) == {"alive"}

    def test_prune_does_not_touch_job_dirs(self, store, cache_root):
        """清理只动元数据，绝不删缓存目录"""
        _make_job_dir(cache_root, "alive")
        store.set_page("orphan", 1, PageMeta(cache_path="o.jpg", width=1, height=2))
        store.set_page("alive", 1, PageMeta(cache_path="a.jpg", width=1, height=2))

        store.prune_orphans()

        assert os.path.isdir(os.path.join(cache_root, "alive"))
        assert os.path.exists(os.path.join(cache_root, "page_meta.json"))
