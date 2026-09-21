"""
cache_manager 单元测试 — HP Scan Tool v4.0
覆盖: 基本 CRUD / FIFO eviction / 活跃任务保护 / stale 清理 / 线程安全 / 配置 TTL
"""

import json
import os
import threading
import time

import pytest

import cache_manager


# ── 基本操作 ──────────────────────────────────────────────


class TestWriteReadPage:
    def test_write_and_read_roundtrip(self, isolated_cache):
        path = cache_manager.write_page("job1", 1, b"hello", "jpg")
        assert os.path.exists(path)
        assert cache_manager.read_page("job1", 1, "jpg") == b"hello"

    def test_write_returns_correct_path(self, isolated_cache):
        path = cache_manager.write_page("job1", 3, b"data", "png")
        expected = os.path.join(isolated_cache, "job1", "page_003.png")
        assert path == expected

    def test_read_nonexistent_raises(self, isolated_cache):
        with pytest.raises(FileNotFoundError):
            cache_manager.read_page("no_job", 1, "jpg")

    def test_write_multiple_pages(self, isolated_cache):
        for i in range(1, 4):
            cache_manager.write_page("job1", i, f"page{i}".encode(), "jpg")
        for i in range(1, 4):
            assert cache_manager.read_page("job1", i, "jpg") == f"page{i}".encode()


class TestListPages:
    def test_empty_for_nonexistent_job(self, isolated_cache):
        assert cache_manager.list_pages("no_job") == []

    def test_returns_sorted_pages(self, isolated_cache):
        cache_manager.write_page("job1", 3, b"c", "jpg")
        cache_manager.write_page("job1", 1, b"a", "jpg")
        cache_manager.write_page("job1", 2, b"b", "png")
        pages = cache_manager.list_pages("job1")
        assert len(pages) == 3
        assert pages[0][0] == 1  # page_num
        assert pages[0][1] == "jpg"
        assert pages[1][0] == 2
        assert pages[1][1] == "png"
        assert pages[2][0] == 3

    def test_list_after_remove_returns_empty(self, isolated_cache):
        cache_manager.write_page("job1", 1, b"data", "jpg")
        cache_manager.remove_job("job1")
        assert cache_manager.list_pages("job1") == []


class TestRemoveJob:
    def test_removes_directory(self, isolated_cache):
        cache_manager.write_page("job1", 1, b"data", "jpg")
        job_path = os.path.join(isolated_cache, "job1")
        assert os.path.isdir(job_path)
        cache_manager.remove_job("job1")
        assert not os.path.exists(job_path)

    def test_remove_nonexistent_is_noop(self, isolated_cache):
        cache_manager.remove_job("nonexistent")  # should not raise


class TestCacheSize:
    def test_empty_cache_is_zero(self, isolated_cache):
        assert cache_manager._cache_size_bytes() == 0

    def test_reflects_written_data(self, isolated_cache):
        cache_manager.write_page("job1", 1, b"x" * 100, "jpg")
        assert cache_manager._cache_size_bytes() == 100

    def test_mb_conversion(self, isolated_cache):
        cache_manager.write_page("job1", 1, b"x" * 1048576, "jpg")
        assert cache_manager._cache_size_mb() == 1.0

    def test_size_decreases_after_remove(self, isolated_cache):
        cache_manager.write_page("job1", 1, b"x" * 100, "jpg")
        cache_manager.remove_job("job1")
        assert cache_manager._cache_size_bytes() == 0


# ── 活跃任务保护 ──────────────────────────────────────────


class TestActiveJobProtection:
    def test_register_and_unregister(self, isolated_cache):
        cache_manager.register_job("job1")
        assert "job1" in cache_manager._active_jobs
        cache_manager.unregister_job("job1")
        assert "job1" not in cache_manager._active_jobs

    def test_unregister_nonexistent_is_noop(self, isolated_cache):
        cache_manager.unregister_job("nonexistent")  # should not raise

    def test_remove_job_clears_active(self, isolated_cache):
        cache_manager.register_job("job1")
        cache_manager.remove_job("job1")
        assert "job1" not in cache_manager._active_jobs

    def test_active_job_protected_from_eviction(self, isolated_cache):
        """设置极小缓存上限，注册 job_b 为活跃，触发清理后 job_b 应保留"""
        # 写入小配置: 1KB 上限, 50% 触发, 50B 清理
        cfg_path = os.path.join(isolated_cache, "cache_config.json")
        with open(cfg_path, "w") as f:
            json.dump({
                "cache_limit_mb": 1024 / (1024 * 1024),  # 1KB
                "trigger_ratio": 0.5,
                "cleanup_mb": 50 / (1024 * 1024),
                "stale_hours": 24,
            }, f)
        # 重置配置缓存以读取新配置
        cache_manager._config_cache = None
        cache_manager._config_cache_time = 0

        # job_a: 旧目录, 500 bytes
        os.makedirs(os.path.join(isolated_cache, "job_a"))
        with open(os.path.join(isolated_cache, "job_a", "page_001.jpg"), "wb") as f:
            f.write(b"a" * 500)
        time.sleep(0.05)

        # job_b: 新目录, 活跃, 300 bytes
        cache_manager.register_job("job_b")
        os.makedirs(os.path.join(isolated_cache, "job_b"))
        with open(os.path.join(isolated_cache, "job_b", "page_001.jpg"), "wb") as f:
            f.write(b"b" * 300)

        # 触发清理（通过 write_page 的 pre-write 检查）
        cache_manager.write_page("job_c", 1, b"c" * 300, "jpg")

        # job_a 应被清理（最旧且非活跃）
        assert not os.path.exists(os.path.join(isolated_cache, "job_a"))
        # job_b 应保留（活跃任务）
        assert os.path.exists(os.path.join(isolated_cache, "job_b"))


# ── FIFO 清理 ─────────────────────────────────────────────


class TestFIFOEviction:
    def _setup_small_config(self, isolated_cache):
        """写入小缓存配置并重置缓存"""
        cfg_path = os.path.join(isolated_cache, "cache_config.json")
        with open(cfg_path, "w") as f:
            json.dump({
                "cache_limit_mb": 1024 / (1024 * 1024),  # 1KB
                "trigger_ratio": 0.5,
                "cleanup_mb": 50 / (1024 * 1024),  # 清理 50B
                "stale_hours": 24,
            }, f)
        cache_manager._config_cache = None
        cache_manager._config_cache_time = 0

    def test_evicts_oldest_job_first(self, isolated_cache):
        self._setup_small_config(isolated_cache)

        # 创建两个旧目录
        os.makedirs(os.path.join(isolated_cache, "old_a"))
        with open(os.path.join(isolated_cache, "old_a", "page_001.jpg"), "wb") as f:
            f.write(b"a" * 400)
        time.sleep(0.05)

        os.makedirs(os.path.join(isolated_cache, "old_b"))
        with open(os.path.join(isolated_cache, "old_b", "page_001.jpg"), "wb") as f:
            f.write(b"b" * 400)

        # 触发清理: 当前 800B > 512B 触发线
        cache_manager.write_page("new", 1, b"c" * 100, "jpg")

        # old_a（最旧）应被清理
        assert not os.path.exists(os.path.join(isolated_cache, "old_a"))

    def test_no_eviction_below_trigger(self, isolated_cache):
        self._setup_small_config(isolated_cache)

        # 写入少量数据（低于触发线 512B）
        cache_manager.write_page("job1", 1, b"x" * 100, "jpg")

        # 目录应保留
        assert os.path.exists(os.path.join(isolated_cache, "job1"))


# ── Stale 清理 ────────────────────────────────────────────


class TestCleanupStale:
    def test_removes_old_directories(self, isolated_cache):
        # 创建目录并设置旧 mtime（25 小时前）
        d = os.path.join(isolated_cache, "stale_job")
        os.makedirs(d)
        with open(os.path.join(d, "page_001.jpg"), "wb") as f:
            f.write(b"old")
        old_time = time.time() - 25 * 3600
        os.utime(d, (old_time, old_time))

        cache_manager.cleanup_stale()
        assert not os.path.exists(d)

    def test_keeps_recent_directories(self, isolated_cache):
        d = os.path.join(isolated_cache, "recent_job")
        os.makedirs(d)
        with open(os.path.join(d, "page_001.jpg"), "wb") as f:
            f.write(b"new")

        cache_manager.cleanup_stale()
        assert os.path.exists(d)


# ── 线程安全 ──────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_writes(self, isolated_cache):
        errors = []

        def writer(job_id, page_num):
            try:
                cache_manager.write_page(job_id, page_num,
                                         b"x" * 10, "jpg")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(f"job{i}", 1))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        for i in range(10):
            pages = cache_manager.list_pages(f"job{i}")
            assert len(pages) == 1

    def test_concurrent_register_unregister(self, isolated_cache):
        errors = []

        def toggle(job_id):
            try:
                for _ in range(50):
                    cache_manager.register_job(job_id)
                    cache_manager.unregister_job(job_id)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=toggle, args=(f"job{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


# ── 配置 TTL ──────────────────────────────────────────────


class TestConfigTTL:
    def test_default_config_values(self, isolated_cache):
        cfg = cache_manager.load_cache_config()
        assert cfg["cache_limit_mb"] == 1024
        assert cfg["trigger_ratio"] == 0.95
        assert cfg["cleanup_mb"] == 150
        assert cfg["stale_hours"] == 24

    def test_config_cached_within_ttl(self, isolated_cache):
        cfg1 = cache_manager.load_cache_config()
        cfg2 = cache_manager.load_cache_config()
        # 两次调用应返回相同值（来自缓存）
        assert cfg1 == cfg2

    def test_save_and_load_config(self, isolated_cache):
        new_cfg = {
            "cache_limit_mb": 512,
            "trigger_ratio": 0.9,
            "cleanup_mb": 100,
            "stale_hours": 12,
        }
        cache_manager.save_cache_config(new_cfg)
        loaded = cache_manager.load_cache_config()
        assert loaded["cache_limit_mb"] == 512
        assert loaded["trigger_ratio"] == 0.9
