"""
终审 Minor 修复守护用例 —— page_meta（F-13 两个模块级容器只增不减）

被守护的行为
------------
1) **进程内写锁表不得无限增长**
   `_WRITE_LOCKS` 按"元数据文件路径"分桶。以前每换一个新的缓存根
   （临时目录 / 新盘 / 换用户）就多留一把 Lock，进程活多久就留多久
   ⇒ 长跑进程内存只增不减。

2) **损坏备份去重表不得无限增长**
   `_CORRUPT_BACKED_UP` 每次"损坏版本变了"就多一个 key，永不回收。

3) **清理不得误伤正在用的东西**
   - 事务**进行中**时，锁表条目必须还在（否则两个实例会拿到两把不同的锁
     ⇒ 读-改-写交错 ⇒ 丢更新）；
   - 还在缓存目录里的 job，元数据**不得**被当成孤儿删掉；
   - 缓存目录已被删掉的 job，元数据**必须**跟着删（job 生命周期一致）。

★ 非空转验证：每条断言都盯住可观测状态（容器长度 / 落盘内容 /
  并发结果），把实现改坏必然变红。
"""

import os
import threading
import time

import pytest

import page_meta
from page_meta import PageMeta, PageMetaStore


@pytest.fixture
def cache_root(tmp_path):
    root = str(tmp_path / "cache")
    os.makedirs(root, exist_ok=True)
    return root


@pytest.fixture
def store(cache_root):
    return PageMetaStore(cache_root)


def _make_job_dir(cache_root, job_id):
    d = os.path.join(cache_root, job_id)
    os.makedirs(d, exist_ok=True)
    return d


# ── 出口 1：进程内写锁表 ────────────────────────────────────────


class TestWriteLockTableBounded:
    def test_lock_table_does_not_grow_with_distinct_paths(self, tmp_path):
        """★ 50 个不同的元数据路径 ⇒ 锁表不得随之增长。"""
        before = len(page_meta._WRITE_LOCKS)

        for i in range(50):
            root = str(tmp_path / ("cache_%02d" % i))
            os.makedirs(root, exist_ok=True)
            PageMetaStore(root).set_page(
                "job1", 1, PageMeta(cache_path="p.jpg", width=1, height=2))

        after = len(page_meta._WRITE_LOCKS)
        assert after - before <= 1, (
            "进程内写锁表随路径数增长（%d → %d）—— 换一次缓存根就永久留下一把锁"
            % (before, after))

    def test_lock_entry_lives_while_transaction_is_open(self, cache_root):
        """★ 事务进行中锁表条目必须在（否则跨实例会拿到两把不同的锁）。"""
        before = len(page_meta._WRITE_LOCKS)

        with PageMetaStore(cache_root)._transaction() as acquired:
            assert acquired is True
            assert len(page_meta._WRITE_LOCKS) > before, (
                "事务进行中锁表条目被提前回收了")

        assert len(page_meta._WRITE_LOCKS) == before, (
            "事务结束后锁表条目没有回收 —— 只增不减")

    def test_same_path_still_shares_one_lock_between_instances(self, cache_root,
                                                                monkeypatch):
        """★ 同一路径的两个 PageMetaStore 实例必须共用同一把进程内锁。

        判据：B 在 A 持锁期间**不能**走到"跨进程文件锁"那一步
        （走到了 ⇒ 说明进程内锁没共享，两把不同的锁可以交错读-改-写）。
        """
        import config_io

        a = PageMetaStore(cache_root)
        b = PageMetaStore(cache_root)

        entered = []
        real = config_io.config_write_lock

        def spy(path):
            entered.append(path)
            return real(path)

        monkeypatch.setattr(config_io, "config_write_lock", spy)

        started = threading.Event()
        done = []

        def writer():
            started.set()
            b.set_page("jobB", 1, PageMeta(cache_path="b.jpg", width=1, height=2))
            done.append(1)

        with a._transaction():
            t = threading.Thread(target=writer)
            t.start()
            assert started.wait(timeout=10)
            time.sleep(0.5)
            assert len(entered) == 1, (
                "B 绕过了进程内锁直接去抢跨进程文件锁 —— 两把不同的锁会交错")
            assert done == [], "B 不应该在 A 持锁期间完成写入"

        t.join(timeout=30)
        assert done == [1]
        assert len(entered) == 2, "B 完成后也必须真的走过跨进程文件锁"

    def test_concurrent_writes_on_same_path_still_do_not_lose_updates(self, cache_root):
        """★ 并发写不丢更新（锁表回收不得破坏串行化）。"""
        store = PageMetaStore(cache_root)
        errors = []
        barrier = threading.Barrier(2)

        def writer(tag):
            try:
                barrier.wait(timeout=30)
                for i in range(15):
                    store.set_page("job_%s" % tag, i, PageMeta(
                        cache_path="%s-%d.jpg" % (tag, i), width=1, height=2))
            except Exception as e:      # pragma: no cover - 出错要看得见
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        assert errors == []
        metas = store.load()
        assert set(metas) == {"job_A", "job_B"}
        assert len(metas["job_A"]) == 15
        assert len(metas["job_B"]) == 15


# ── 出口 2：损坏备份去重表 ──────────────────────────────────────


class TestCorruptBackupTableBounded:
    def test_backup_table_does_not_grow_unbounded(self, tmp_path):
        """★ 200 次"损坏内容各不相同" ⇒ 去重表必须有上限。"""
        root = str(tmp_path / "cache")
        os.makedirs(root, exist_ok=True)
        store = PageMetaStore(root)

        for i in range(200):
            with open(store._path, "w", encoding="utf-8") as f:
                f.write("{" + ("x" * (i + 1)))     # 每次内容不同 ⇒ key 不同
            store.load()                            # 触发损坏备份

        assert len(page_meta._CORRUPT_BACKED_UP) <= 128, (
            "损坏备份去重表无限增长（%d 条）" % len(page_meta._CORRUPT_BACKED_UP))

    def test_same_corrupt_content_is_backed_up_only_once(self, tmp_path):
        """★ 去重语义不能被清理策略破坏（同一份损坏内容只备份一次）。"""
        root = str(tmp_path / "cache")
        os.makedirs(root, exist_ok=True)
        store = PageMetaStore(root)

        with open(store._path, "w", encoding="utf-8") as f:
            f.write("{not json")
        for _ in range(5):
            store.load()

        backups = [n for n in os.listdir(root) if ".corrupt-" in n]
        assert len(backups) == 1, (
            "同一份损坏内容被反复备份：%r" % (backups,))


# ── 出口 3：job 生命周期一致（目录没了 ⇒ 元数据也没了）──────────


class TestJobLifecycleConsistency:
    def test_meta_of_deleted_job_dir_is_removed(self, store, cache_root):
        """job 目录被删 ⇒ 元数据必须跟着删（prune_orphans）。"""
        _make_job_dir(cache_root, "gone")
        store.set_page("gone", 1, PageMeta(cache_path="g.jpg", width=1, height=2))

        import shutil
        shutil.rmtree(os.path.join(cache_root, "gone"))

        removed = store.prune_orphans()
        assert "gone" in removed
        assert "gone" not in store.load()

    def test_meta_of_live_job_is_never_removed(self, store, cache_root):
        """★ 还在用的 job 的元数据不得被误删。"""
        _make_job_dir(cache_root, "alive")
        store.set_page("alive", 1, PageMeta(cache_path="a.jpg", width=1, height=2))

        store.prune_orphans()

        assert store.get_job("alive")["1"]["cache_path"] == "a.jpg"

    def test_remove_job_drops_meta_and_prunes_orphans(self, store, cache_root):
        """remove_job：删掉本 job 的元数据，并顺手清掉别的孤儿。"""
        _make_job_dir(cache_root, "alive")
        store.set_page("orphan", 1, PageMeta(cache_path="o.jpg", width=1, height=2))
        store.set_page("alive", 1, PageMeta(cache_path="a.jpg", width=1, height=2))

        store.remove_job("alive")

        metas = store.load()
        assert "alive" not in metas
        assert "orphan" not in metas

    def test_prune_is_idempotent(self, store, cache_root):
        _make_job_dir(cache_root, "alive")
        store.set_page("orphan", 1, PageMeta(cache_path="o.jpg", width=1, height=2))

        assert store.prune_orphans() == ["orphan"]
        assert store.prune_orphans() == []
        time.sleep(0.01)
