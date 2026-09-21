"""
history_manager 单元测试 — HP Scan Tool v4.0
覆盖: 追加/列表/清空 / 500 条上限 / 线程安全 / 自动时间戳
"""

import json
import os
import threading

import pytest

import history_manager


# ── 基本操作 ──────────────────────────────────────────────


class TestAppend:
    def test_append_single_entry(self, tmp_config_dir):
        history_manager.append({"device_ip": "1.2.3.4", "page_count": 1})
        records = history_manager.list_records()
        assert len(records) == 1
        assert records[0]["device_ip"] == "1.2.3.4"

    def test_auto_timestamp(self, tmp_config_dir):
        history_manager.append({"device_ip": "1.2.3.4"})
        records = history_manager.list_records()
        assert "timestamp" in records[0]
        assert len(records[0]["timestamp"]) > 0

    def test_custom_timestamp_preserved(self, tmp_config_dir):
        history_manager.append({
            "device_ip": "1.2.3.4",
            "timestamp": "2025-01-01T00:00:00",
        })
        records = history_manager.list_records()
        assert records[0]["timestamp"] == "2025-01-01T00:00:00"

    def test_append_multiple(self, tmp_config_dir):
        for i in range(5):
            history_manager.append({"device_ip": f"10.0.0.{i}"})
        records = history_manager.list_records()
        assert len(records) == 5


class TestListRecords:
    def test_empty_history(self, tmp_config_dir):
        records = history_manager.list_records()
        assert records == []

    def test_returns_newest_first(self, tmp_config_dir):
        for i in range(3):
            history_manager.append({"order": i})
        records = history_manager.list_records()
        assert records[0]["order"] == 2  # 最新在前
        assert records[2]["order"] == 0

    def test_limit_parameter(self, tmp_config_dir):
        for i in range(10):
            history_manager.append({"idx": i})
        records = history_manager.list_records(limit=3)
        assert len(records) == 3
        # 应返回最新的 3 条
        assert records[0]["idx"] == 9
        assert records[2]["idx"] == 7

    def test_default_limit_50(self, tmp_config_dir):
        for i in range(60):
            history_manager.append({"idx": i})
        records = history_manager.list_records()
        assert len(records) == 50
        # 最新的应在前面
        assert records[0]["idx"] == 59


class TestClear:
    def test_clear_empties_history(self, tmp_config_dir):
        for i in range(3):
            history_manager.append({"idx": i})
        history_manager.clear()
        assert history_manager.list_records() == []

    def test_clear_on_empty_is_noop(self, tmp_config_dir):
        history_manager.clear()
        assert history_manager.list_records() == []


# ── 500 条上限 ────────────────────────────────────────────


class TestRecordLimit:
    def test_500_record_cap(self, tmp_config_dir):
        """追加 510 条后应只保留最近 500 条"""
        for i in range(510):
            history_manager.append({"idx": i})
        records = history_manager.list_records(limit=9999)
        assert len(records) == 500
        # 最旧的保留记录应为 idx=10
        oldest = records[-1]
        assert oldest["idx"] == 10

    def test_exactly_500_no_truncation(self, tmp_config_dir):
        for i in range(500):
            history_manager.append({"idx": i})
        records = history_manager.list_records(limit=9999)
        assert len(records) == 500


# ── 线程安全 ──────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_appends(self, tmp_config_dir):
        errors = []

        def writer(idx):
            try:
                history_manager.append({"thread_idx": idx})
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(i,))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        records = history_manager.list_records(limit=9999)
        assert len(records) == 20


# ── 容错 ──────────────────────────────────────────────────


class TestRobustness:
    def test_corrupt_json_handled(self, tmp_config_dir):
        path = history_manager._history_file()
        with open(path, "w") as f:
            f.write("not json")
        records = history_manager.list_records()
        assert records == []

    def test_non_list_json_handled(self, tmp_config_dir):
        path = history_manager._history_file()
        with open(path, "w") as f:
            json.dump({"not": "a list"}, f)
        records = history_manager.list_records()
        assert records == []

    def test_append_after_corrupt_file(self, tmp_config_dir):
        path = history_manager._history_file()
        with open(path, "w") as f:
            f.write("corrupt")
        history_manager.append({"device_ip": "1.2.3.4"})
        records = history_manager.list_records()
        assert len(records) == 1
