"""
cache_manager 目录保护单元测试 — HP Scan Tool v4.0（B4-2）

覆盖:
  - protect_dir / unprotect_dir / is_job_protected 三个接口
  - 两条自动清理路径（_evict_oldest_jobs_locked / cleanup_stale）都跳过受保护目录
  - remove_job() 的"显式删除优先于保护"语义不变
  - 既有保护来源（_active_jobs / _pending_jobs）不回归
  - V1-N-3：eviction 的 freed 统计走递归口径（含子目录的 job 目录）
  - V1-N-5：job_id 越界缓存根时拒绝建/删目录
"""

import json
import os
import time

import pytest

import cache_manager


@pytest.fixture(autouse=True)
def _isolated_protected_dirs(monkeypatch):
    """
    每个用例一份独立的显式保护登记。

    ★ conftest 的 isolated_cache 只重置 _active_jobs / _pending_jobs，
      不含 _protected_dirs ⇒ 这里单独重置，避免用例间互相污染。
    """
    monkeypatch.setattr(cache_manager, "_protected_dirs", {})


def _setup_small_config(isolated_cache):
    """写入极小缓存配置并重置配置缓存（1KB 上限 / 50% 触发 / 50B 清理）"""
    cfg_path = os.path.join(isolated_cache, "cache_config.json")
    with open(cfg_path, "w") as f:
        json.dump({
            "cache_limit_mb": 1024 / (1024 * 1024),   # 1KB
            "trigger_ratio": 0.5,
            "cleanup_mb": 50 / (1024 * 1024),          # 50B
            "stale_hours": 24,
        }, f)
    cache_manager._config_cache = None
    cache_manager._config_cache_time = 0


def _actual_size(root):
    """磁盘上真实占用（递归）—— 用作增量计数的真值对照"""
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


# ── 接口契约 ──────────────────────────────────────────────


class TestProtectDirApi:
    def test_protect_then_unprotect(self, isolated_cache):
        cache_manager.protect_dir("job_x")
        assert cache_manager.is_job_protected("job_x")
        cache_manager.unprotect_dir("job_x")
        assert not cache_manager.is_job_protected("job_x")

    def test_protect_is_idempotent(self, isolated_cache):
        """重复 protect_dir 不报错、不产生副作用"""
        cache_manager.protect_dir("job_x")
        cache_manager.protect_dir("job_x")
        assert cache_manager.is_job_protected("job_x")
        assert "job_x" in cache_manager.protected_job_ids()

    def test_unprotect_unknown_name_is_noop(self, isolated_cache):
        """对没保护过的名字调 unprotect_dir 不报错"""
        cache_manager.unprotect_dir("never_protected")
        assert not cache_manager.is_job_protected("never_protected")

    def test_unprotect_is_idempotent(self, isolated_cache):
        cache_manager.protect_dir("job_x")
        cache_manager.unprotect_dir("job_x")
        cache_manager.unprotect_dir("job_x")
        assert not cache_manager.is_job_protected("job_x")

    def test_protect_does_not_create_directory(self, isolated_cache):
        """protect_dir 只是登记保护，不建目录"""
        cache_manager.protect_dir("job_x")
        assert not os.path.exists(os.path.join(isolated_cache, "job_x"))

    def test_unprotect_does_not_delete_directory(self, isolated_cache):
        """unprotect_dir 只解除保护，不删目录"""
        cache_manager.write_page("job_x", 1, b"data", "jpg")
        cache_manager.protect_dir("job_x")
        cache_manager.unprotect_dir("job_x")
        assert os.path.isdir(os.path.join(isolated_cache, "job_x"))

    def test_is_job_protected_covers_three_sources(self, isolated_cache):
        """受保护 = 活跃 | 待保存 | 显式保护"""
        cache_manager.register_job("job_active")
        cache_manager.register_pending_job("job_pending")
        cache_manager.protect_dir("job_explicit")

        assert cache_manager.is_job_protected("job_active")
        assert cache_manager.is_job_protected("job_pending")
        assert cache_manager.is_job_protected("job_explicit")
        assert not cache_manager.is_job_protected("job_other")

    def test_protected_job_ids_includes_explicit(self, isolated_cache):
        cache_manager.protect_dir("job_explicit")
        assert "job_explicit" in cache_manager.protected_job_ids()

    def test_protection_does_not_leak_across_cache_roots(self, isolated_cache,
                                                         tmp_path, monkeypatch):
        """
        显式保护**按缓存根分组** ⇒ 别处登记的同名 job 不应影响本缓存根。

        ★ 这是一条真实的跨文件污染护栏：document_session 的测试会调用
          真正的 protect_dir（登记若干 job 名），若保护集合是扁平的，
          那些名字会泄漏到其它用例的缓存根上 ⇒ eviction 行为随执行顺序漂移。
        """
        # 在"另一个缓存根"下登记同名 job
        other_root = str(tmp_path / "other_cache")
        os.makedirs(other_root, exist_ok=True)
        monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: other_root)
        cache_manager._config_cache = None
        cache_manager._config_cache_time = 0
        cache_manager.protect_dir("job_a")

        # 回到本用例的缓存根
        monkeypatch.setattr(cache_manager, "_default_cache_root",
                            lambda: isolated_cache)
        cache_manager._config_cache = None
        cache_manager._config_cache_time = 0

        assert not cache_manager.is_job_protected("job_a"), (
            "别的缓存根下登记的保护泄漏到了本缓存根")
        assert "job_a" not in cache_manager.protected_job_ids()


# ── eviction 跳过受保护目录 ───────────────────────────────


class TestEvictionSkipsProtected:
    def _make_old_job(self, root, name, size):
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "page_001.jpg"), "wb") as f:
            f.write(b"x" * size)
        return d

    def test_protected_dir_survives_eviction(self, isolated_cache):
        """受保护目录即便最旧（会被 FIFO 第一个删）也必须留下"""
        _setup_small_config(isolated_cache)

        prot = self._make_old_job(isolated_cache, "job_prot", 500)
        time.sleep(0.05)
        old = self._make_old_job(isolated_cache, "job_old", 400)
        cache_manager.protect_dir("job_prot")

        # 触发 eviction（write_page 写入前会先做清理检查）
        cache_manager.write_page("job_trigger", 1, b"t" * 300, "jpg")

        assert os.path.exists(prot), "受保护目录被 eviction 删掉了"
        assert not os.path.exists(old), "未受保护的最旧目录应被 eviction 清掉"

    def test_unprotected_dir_can_be_evicted(self, isolated_cache):
        """unprotect_dir 之后该目录可以被正常清理"""
        _setup_small_config(isolated_cache)

        prot = self._make_old_job(isolated_cache, "job_prot", 500)
        time.sleep(0.05)
        self._make_old_job(isolated_cache, "job_old", 400)

        cache_manager.protect_dir("job_prot")
        cache_manager.unprotect_dir("job_prot")

        cache_manager.write_page("job_trigger", 1, b"t" * 300, "jpg")

        assert not os.path.exists(prot), "解除保护后目录应可被 eviction 清理"


# ── cleanup_stale 跳过受保护目录 ──────────────────────────


class TestCleanupStaleSkipsProtected:
    def _make_stale_job(self, root, name, age_hours=100):
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "page_001.jpg"), "wb") as f:
            f.write(b"old")
        old = time.time() - age_hours * 3600
        os.utime(d, (old, old))
        return d

    def test_protected_dir_survives_cleanup_stale(self, isolated_cache):
        """受保护目录即便超过 stale_hours（默认 24h）也必须留下"""
        prot = self._make_stale_job(isolated_cache, "job_prot")
        stale = self._make_stale_job(isolated_cache, "job_stale")
        cache_manager.protect_dir("job_prot")

        cache_manager.cleanup_stale()

        assert os.path.exists(prot), "受保护目录被 stale 清理删掉了"
        assert not os.path.exists(stale), "未受保护的过期目录应被清理"

    def test_unprotected_dir_can_be_cleaned(self, isolated_cache):
        """unprotect_dir 之后该目录可以被 cleanup_stale 清理"""
        prot = self._make_stale_job(isolated_cache, "job_prot")

        cache_manager.protect_dir("job_prot")
        cache_manager.unprotect_dir("job_prot")

        cache_manager.cleanup_stale()

        assert not os.path.exists(prot), "解除保护后目录应可被 stale 清理"


# ── remove_job：显式删除优先于保护 ───────────────────────


class TestRemoveJobWinsOverProtection:
    def test_remove_job_deletes_protected_dir(self, isolated_cache):
        """显式删除永远优先于保护 —— 受保护的目录照样删掉"""
        cache_manager.write_page("job_prot", 1, b"data", "jpg")
        cache_manager.protect_dir("job_prot")
        assert cache_manager.is_job_protected("job_prot")

        cache_manager.remove_job("job_prot")

        assert not os.path.exists(os.path.join(isolated_cache, "job_prot"))

    def test_remove_job_still_clears_active_and_pending(self, isolated_cache):
        """既有语义不变：remove_job 解除活跃 / 待保存保护"""
        cache_manager.register_job("job_a")
        cache_manager.register_pending_job("job_b")

        cache_manager.remove_job("job_a")
        cache_manager.remove_job("job_b")

        assert "job_a" not in cache_manager._active_jobs
        assert "job_b" not in cache_manager._pending_jobs


# ── 既有行为不回归 ────────────────────────────────────────


class TestExistingProtectionUnchanged:
    def test_active_job_still_protected_from_eviction(self, isolated_cache):
        _setup_small_config(isolated_cache)
        cache_manager.register_job("job_active")
        d = os.path.join(isolated_cache, "job_active")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "page_001.jpg"), "wb") as f:
            f.write(b"a" * 500)
        time.sleep(0.05)
        old = os.path.join(isolated_cache, "job_old")
        os.makedirs(old, exist_ok=True)
        with open(os.path.join(old, "page_001.jpg"), "wb") as f:
            f.write(b"o" * 400)

        cache_manager.write_page("job_trigger", 1, b"t" * 300, "jpg")

        assert os.path.exists(d), "活跃任务目录被 eviction 删掉了"
        assert not os.path.exists(old)

    def test_active_job_still_protected_from_cleanup_stale(self, isolated_cache):
        d = os.path.join(isolated_cache, "job_active")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "page_001.jpg"), "wb") as f:
            f.write(b"a")
        old = time.time() - 100 * 3600
        os.utime(d, (old, old))
        cache_manager.register_job("job_active")

        cache_manager.cleanup_stale()

        assert os.path.exists(d), "活跃任务目录被 stale 清理删掉了"

    def test_pending_job_still_protected_from_cleanup_stale(self, isolated_cache):
        d = os.path.join(isolated_cache, "job_pending")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "page_001.jpg"), "wb") as f:
            f.write(b"a")
        old = time.time() - 100 * 3600
        os.utime(d, (old, old))
        cache_manager.register_pending_job("job_pending")

        cache_manager.cleanup_stale()

        assert os.path.exists(d), "待保存任务目录被 stale 清理删掉了"


# ── V1-N-3：eviction 的 freed 统计走递归口径 ──────────────


class TestEvictionFreedCountsNestedFiles:
    def test_freed_counts_subdirectory_bytes(self, isolated_cache):
        """
        job 目录里带子目录时，eviction 统计的 freed 必须**递归**算全。

        旧实现只统计目录第一层的文件 ⇒ freed 被低估 ⇒ 增量计数偏高
        ⇒ eviction 触发线偏早。
        """
        _setup_small_config(isolated_cache)

        d = os.path.join(isolated_cache, "job_nested")
        os.makedirs(os.path.join(d, "sub"), exist_ok=True)
        with open(os.path.join(d, "page_001.jpg"), "wb") as f:
            f.write(b"a" * 100)
        with open(os.path.join(d, "sub", "page_002.jpg"), "wb") as f:
            f.write(b"b" * 900)

        # 先建立增量计数的基线（一次全量 walk）
        cache_manager._cache_size_bytes()

        # 触发 eviction：该 job 目录会被整棵子树删掉
        cache_manager.write_page("job_trigger", 1, b"t" * 100, "jpg")

        assert not os.path.exists(d), "job_nested 应被 eviction 删除"

        # 增量计数必须回到磁盘真值
        assert cache_manager._cache_size_cache is not None
        assert cache_manager._cache_size_cache[0] == _actual_size(isolated_cache), (
            "增量计数与磁盘真值不符（freed 统计口径不一致）")


# ── V1-N-5：job_id 越界缓存根 ────────────────────────────


class TestJobIdCannotEscapeCacheRoot:
    def test_write_page_rejects_escaping_job_id(self, isolated_cache):
        """job_id = '../escape' ⇒ 绝不在缓存根之外创建目录"""
        parent = os.path.dirname(isolated_cache)
        with pytest.raises(ValueError):
            cache_manager.write_page("../escape", 1, b"data", "jpg")

        assert not os.path.isdir(os.path.join(parent, "escape"))

    def test_read_page_rejects_escaping_job_id(self, isolated_cache):
        with pytest.raises(ValueError):
            cache_manager.read_page("../escape", 1, "jpg")

    def test_list_pages_rejects_escaping_job_id(self, isolated_cache):
        with pytest.raises(ValueError):
            cache_manager.list_pages("../escape")

    def test_remove_job_refuses_escaping_job_id(self, isolated_cache):
        """remove_job 越界时拒绝删除（且不抛异常 —— 它在落盘主路径上）"""
        parent = os.path.dirname(isolated_cache)
        outside = os.path.join(parent, "escape")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "keep.txt"), "wb") as f:
            f.write(b"keep")

        cache_manager.remove_job("../escape")

        assert os.path.isdir(outside), "越界目录被删了"
        assert os.path.exists(os.path.join(outside, "keep.txt"))

    def test_normal_job_id_still_works(self, isolated_cache):
        """正常 job_id 行为完全不变"""
        path = cache_manager.write_page("20260101_120000_123456", 1, b"data", "jpg")
        assert os.path.exists(path)
        assert cache_manager.read_page("20260101_120000_123456", 1, "jpg") == b"data"
        assert len(cache_manager.list_pages("20260101_120000_123456")) == 1
        cache_manager.remove_job("20260101_120000_123456")
        assert not os.path.exists(path)
