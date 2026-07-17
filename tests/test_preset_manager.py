"""
preset_manager 单元测试 — HP Scan Tool v3.3
覆盖: 加载内置/用户预设 / 保存覆盖 / 删除 / 导出导入
"""

import json
import os

import pytest

import preset_manager


# ── 加载预设 ──────────────────────────────────────────────


class TestLoadPresets:
    def test_load_builtin_only(self, tmp_config_dir):
        presets = preset_manager.load_presets()
        assert len(presets) == 4
        names = [p["name"] for p in presets]
        assert "文字增强" in names
        assert "照片还原" in names
        assert "去灰底" in names
        assert "旧文件修复" in names
        assert all(p["builtin"] is True for p in presets)

    def test_load_builtin_plus_user(self, tmp_config_dir):
        # 先保存一个用户预设
        preset_manager.save_user_preset({
            "name": "我的预设",
            "mode": "manual",
            "brightness": 10, "contrast": 20,
            "gamma": 1.0, "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        })
        presets = preset_manager.load_presets()
        assert len(presets) == 5
        user_presets = [p for p in presets if not p["builtin"]]
        assert len(user_presets) == 1
        assert user_presets[0]["name"] == "我的预设"

    def test_load_handles_corrupt_file(self, tmp_config_dir):
        path = preset_manager._presets_path()
        with open(path, "w") as f:
            f.write("not json")
        presets = preset_manager.load_presets()
        # 应只返回内置预设
        assert len(presets) == 4

    def test_load_ignores_non_list_json(self, tmp_config_dir):
        path = preset_manager._presets_path()
        with open(path, "w") as f:
            json.dump({"not": "a list"}, f)
        presets = preset_manager.load_presets()
        assert len(presets) == 4


# ── 保存预设 ──────────────────────────────────────────────


class TestSaveUserPreset:
    def test_save_new_preset(self, tmp_config_dir):
        preset_manager.save_user_preset({
            "name": "测试预设",
            "mode": "auto",
            "brightness": 5, "contrast": 10,
            "gamma": 1.0, "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        })
        presets = preset_manager.load_presets()
        user = [p for p in presets if not p["builtin"]]
        assert len(user) == 1
        assert user[0]["name"] == "测试预设"

    def test_save_strips_builtin_flag(self, tmp_config_dir):
        preset_manager.save_user_preset({
            "name": "测试",
            "mode": "off",
            "brightness": 0, "contrast": 0,
            "gamma": 1.0, "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
            "builtin": True,
        })
        # 直接读取 JSON 文件确认 builtin 已移除
        path = preset_manager._presets_path()
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "builtin" not in data[0]

    def test_overwrite_same_name(self, tmp_config_dir):
        preset_manager.save_user_preset({
            "name": "同名",
            "mode": "auto",
            "brightness": 10, "contrast": 10,
            "gamma": 1.0, "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        })
        preset_manager.save_user_preset({
            "name": "同名",
            "mode": "manual",
            "brightness": 99, "contrast": 99,
            "gamma": 2.0, "shadows": 10, "highlights": -10,
            "channel_gains": [1.1, 0.9, 1.0],
        })
        presets = preset_manager.load_presets()
        user = [p for p in presets if not p["builtin"]]
        assert len(user) == 1
        assert user[0]["brightness"] == 99
        assert user[0]["gamma"] == 2.0


# ── 删除预设 ──────────────────────────────────────────────


class TestDeleteUserPreset:
    def test_delete_existing_user_preset(self, tmp_config_dir):
        preset_manager.save_user_preset({
            "name": "要删的",
            "mode": "off",
            "brightness": 0, "contrast": 0,
            "gamma": 1.0, "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        })
        preset_manager.delete_user_preset("要删的")
        presets = preset_manager.load_presets()
        assert len(presets) == 4  # 只剩内置

    def test_delete_nonexistent_is_noop(self, tmp_config_dir):
        preset_manager.delete_user_preset("不存在")
        presets = preset_manager.load_presets()
        assert len(presets) == 4

    def test_builtin_not_affected(self, tmp_config_dir):
        """delete_user_preset 只操作用户预设文件，内置预设不受影响"""
        preset_manager.delete_user_preset("文字增强")
        presets = preset_manager.load_presets()
        names = [p["name"] for p in presets]
        assert "文字增强" in names  # 内置仍在


# ── 导出预设 ──────────────────────────────────────────────


class TestExportPreset:
    def test_export_builtin_preset(self, tmp_config_dir, tmp_path):
        out = str(tmp_path / "exported.json")
        result = preset_manager.export_preset("文字增强", out)
        assert result is True
        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == 1
        assert data["preset"]["name"] == "文字增强"
        assert "builtin" not in data["preset"]

    def test_export_nonexistent(self, tmp_config_dir, tmp_path):
        out = str(tmp_path / "nope.json")
        result = preset_manager.export_preset("不存在", out)
        assert result is False

    def test_export_user_preset(self, tmp_config_dir, tmp_path):
        preset_manager.save_user_preset({
            "name": "用户预设",
            "mode": "manual",
            "brightness": 42, "contrast": 0,
            "gamma": 1.0, "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        })
        out = str(tmp_path / "user_export.json")
        result = preset_manager.export_preset("用户预设", out)
        assert result is True
        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["preset"]["brightness"] == 42


# ── 导入预设 ──────────────────────────────────────────────


class TestImportPreset:
    def test_import_valid(self, tmp_config_dir, tmp_path):
        # 先导出再导入
        preset_manager.save_user_preset({
            "name": "源预设",
            "mode": "auto",
            "brightness": 7, "contrast": 8,
            "gamma": 1.1, "shadows": 1, "highlights": -1,
            "channel_gains": [1.0, 1.0, 1.0],
        })
        export_path = str(tmp_path / "export.json")
        preset_manager.export_preset("源预设", export_path)

        name = preset_manager.import_preset(export_path)
        assert name == "源预设"

        presets = preset_manager.load_presets()
        imported = [p for p in presets if p["name"] == "源预设" and not p["builtin"]]
        assert len(imported) == 1

    def test_import_invalid_json(self, tmp_config_dir, tmp_path):
        bad = str(tmp_path / "bad.json")
        with open(bad, "w") as f:
            f.write("not json")
        result = preset_manager.import_preset(bad)
        assert result is None

    def test_import_missing_preset_key(self, tmp_config_dir, tmp_path):
        bad = str(tmp_path / "nokey.json")
        with open(bad, "w") as f:
            json.dump({"version": 1}, f)
        result = preset_manager.import_preset(bad)
        assert result is None

    def test_import_missing_required_fields(self, tmp_config_dir, tmp_path):
        bad = str(tmp_path / "incomplete.json")
        with open(bad, "w") as f:
            json.dump({"preset": {"brightness": 10}}, f)
        result = preset_manager.import_preset(bad)
        assert result is None

    def test_import_nonexistent_file(self, tmp_config_dir):
        result = preset_manager.import_preset("/nonexistent/path.json")
        assert result is None
