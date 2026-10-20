"""
preset_manager 并发与损坏保护回归测试 — HP Scan Tool v4.0

锁死 m-2（Minor）：
  ① 并发丢更新：旧实现"固定临时名 + 无锁 + 读-改-写不在同一事务内"，
     实测 8 线程 × 25 次并发存 200 个预设，最终只剩 5 个。
  ② 损坏静默覆盖：presets.json 损坏后 load_presets 静默只返回内置 4 条，
     下一次保存直接覆盖原文件 ⇒ 用户预设永久丢失。
     修法与 history_manager._backup_corrupt 对齐：先备份原文 + 记 error 日志，
     并且**禁止**用空列表覆盖原文件。
"""

import json
import os
import threading

import pytest

import preset_manager


def _user_presets(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _corrupt_backups(path):
    """与 history_manager 同风格的备份文件：<name>.corrupt-<时间戳>"""
    directory = os.path.dirname(path)
    base = os.path.basename(path)
    return [f for f in os.listdir(directory)
            if f.startswith(base + ".corrupt-")]


class TestConcurrentSave:
    def test_concurrent_saves_keep_every_preset(self, tmp_config_dir):
        """8 线程 × 25 次并发存 200 个预设 ⇒ 最终一条都不能少"""
        path = preset_manager._presets_path()
        errors = []
        barrier = threading.Barrier(8)

        def writer(tag):
            try:
                barrier.wait(timeout=30)
                for i in range(25):
                    preset_manager.save_user_preset({
                        "name": f"{tag}-{i}",
                        "mode": "auto",
                        "brightness": i,
                        "contrast": i,
                        "gamma": 1.0,
                        "shadows": 0,
                        "highlights": 0,
                        "channel_gains": [1.0, 1.0, 1.0],
                    })
            except Exception as e:      # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,))
                   for t in ("A", "B", "C", "D", "E", "F", "G", "H")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)

        assert errors == [], f"并发保存抛异常: {errors}"
        saved = _user_presets(path)
        assert len(saved) >= 200, f"预设被覆盖了：只剩 {len(saved)} 条"
        names = {p["name"] for p in saved}
        expected = {f"{t}-{i}" for t in "ABCDEFGH" for i in range(25)}
        assert expected <= names, f"丢了 {len(expected - names)} 条预设"

    def test_no_tmp_left_after_concurrent_saves(self, tmp_config_dir):
        """并发写结束后目录里不能有残留 .tmp"""
        path = preset_manager._presets_path()

        def writer(tag):
            for i in range(5):
                preset_manager.save_user_preset({
                    "name": f"{tag}-{i}", "mode": "auto",
                    "brightness": 0, "contrast": 0, "gamma": 1.0,
                    "shadows": 0, "highlights": 0,
                    "channel_gains": [1.0, 1.0, 1.0],
                })

        threads = [threading.Thread(target=writer, args=(t,)) for t in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        leftovers = [f for f in os.listdir(os.path.dirname(path))
                     if f.endswith(".tmp")]
        assert leftovers == [], f"残留临时文件: {leftovers}"


class TestCorruptPresets:
    def test_load_backs_up_corrupt_file(self, tmp_config_dir):
        """损坏的 presets.json：加载时原文必须被备份 + 记 error 日志"""
        path = preset_manager._presets_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")

        presets = preset_manager.load_presets()

        assert len(presets) == 4, "损坏时只应返回内置预设"
        backups = _corrupt_backups(path)
        assert len(backups) == 1, f"损坏原文必须被备份，实际备份: {backups}"
        with open(os.path.join(os.path.dirname(path), backups[0]),
                  "r", encoding="utf-8") as f:
            assert f.read() == "{not json", "备份必须保留原文"

    def test_load_logs_error(self, tmp_config_dir, caplog):
        """损坏必须记 error 日志（不能静默）"""
        import logging
        path = preset_manager._presets_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json")

        with caplog.at_level(logging.ERROR):
            preset_manager.load_presets()

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "损坏必须记 error 日志"
        assert any("presets.json" in r.getMessage() or "预设" in r.getMessage()
                   for r in errors)

    def test_corrupt_file_is_not_silently_overwritten(self, tmp_config_dir):
        """损坏后再保存：原文件既不被覆盖，也不被清空（返回 False 表示没写）"""
        path = preset_manager._presets_path()
        original = "{not json"
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

        ok = preset_manager.save_user_preset({
            "name": "新预设", "mode": "auto",
            "brightness": 0, "contrast": 0, "gamma": 1.0,
            "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        })

        assert ok is False, "原文件损坏时必须明确告知调用方：这次写入没有发生"
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == original, "损坏的原文件不能被静默覆盖"
        backups = _corrupt_backups(path)
        assert len(backups) == 1, "损坏原文必须留有备份"

    def test_delete_on_corrupt_file_is_skipped(self, tmp_config_dir):
        """损坏时删除也必须跳过（不能把原文件覆盖成空列表）"""
        path = preset_manager._presets_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("[broken")

        ok = preset_manager.delete_user_preset("任意")

        assert ok is False
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == "[broken"

    def test_non_list_json_is_treated_as_corrupt(self, tmp_config_dir):
        """顶层不是数组 ⇒ 同样按损坏处理（备份 + 禁止覆盖）"""
        path = preset_manager._presets_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"not": "a list"}, f)

        presets = preset_manager.load_presets()
        assert len(presets) == 4
        assert len(_corrupt_backups(path)) == 1

    def test_empty_file_is_not_treated_as_corrupt(self, tmp_config_dir):
        """空文件没有任何数据可丢 ⇒ 允许正常写入，不产生备份"""
        path = preset_manager._presets_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("")

        ok = preset_manager.save_user_preset({
            "name": "空文件后写入", "mode": "auto",
            "brightness": 0, "contrast": 0, "gamma": 1.0,
            "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        })

        assert ok is True
        assert _corrupt_backups(path) == []
        assert len(_user_presets(path)) == 1


class TestPresetsPath:
    def test_path_stays_in_appdata(self, tmp_path, monkeypatch):
        """预设路径必须留在 %APPDATA%\\HP_Scan_Tool\\presets.json（267a360 统一过）"""
        monkeypatch.setenv("APPDATA", str(tmp_path))
        path = preset_manager._presets_path()
        assert path == os.path.join(str(tmp_path), "HP_Scan_Tool", "presets.json")
        assert os.path.dirname(os.path.dirname(path)) == str(tmp_path), \
            "预设文件不能落回项目根目录"
