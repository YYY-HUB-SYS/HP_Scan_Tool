"""
page_meta 单元测试 — HP Scan Tool v4.0
覆盖: 按 job 存取 / job 隔离 / 原子写 / 损坏文件容错
"""

import json
import os

import pytest

import page_meta
from page_meta import PageMeta, PageMetaStore


@pytest.fixture
def store(tmp_path):
    return PageMetaStore(str(tmp_path / "cache"))


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

    def test_remove_job_only_removes_that_job(self, store):
        store.set_page("jobA", 1, PageMeta(cache_path="a.jpg", width=1, height=2))
        store.set_page("jobB", 1, PageMeta(cache_path="b.jpg", width=3, height=4))
        store.remove_job("jobA")
        assert store.get_job("jobA") == {}
        assert store.get_job("jobB")["1"]["cache_path"] == "b.jpg"

    def test_remove_missing_job_is_noop(self, store):
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
        """写失败时原文件必须完好（原子写：.tmp + os.replace）"""
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

    def test_unicode_path_roundtrip(self, store):
        store.set_page("job1", 1, PageMeta(
            cache_path="C:/扫描/第1页.jpg", width=1, height=2))
        assert store.get_job("job1")["1"]["cache_path"] == "C:/扫描/第1页.jpg"
