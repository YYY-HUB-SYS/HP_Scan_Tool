"""
终审 Minor 修复守护用例 —— preset_manager（F-10 内置预设共享 / F-11 import_preset 两个洞）

被守护的行为
------------
1) **内置预设必须是副本，不是模块级字典本体**
   `load_presets()` 返回的内置预设一旦被调用方就地修改（改 brightness、
   改 channel_gains……），**不得**污染进程内的全局内置预设。
   事故场景（reviewer 实测）：调用方把 brightness 15 改成 999 ⇒
   之后**所有**扫描都用错曝光参数，且没有任何提示。

2) **import_preset 必须校验类型**
   `{"preset": "字符串"}` 这类文件必须**明确失败**（返回 None + error 日志），
   不能抛 AttributeError（PySide6 ≥ 6.5 槽内未捕获异常 ⇒ qFatal ⇒ 进程崩），
   更不能把坏数据写进 presets.json。

3) **import_preset 必须回报成败**
   写盘失败（预设文件已损坏 / 拿不到跨进程锁）时返回 None；
   不能"照样返回预设名"让调用方以为导入成功。

★ 非空转验证的前提：下面每条断言都盯住一个**可观测的输出**
  （全局预设的值 / 函数返回值 / 落盘内容）——把实现改坏必然变红。
"""

import copy
import json
import os

import pytest

import preset_manager


# ── 守护夹具：把内置预设的值还原 ────────────────────────────────
# ★ 内置预设在"实现修好之前"是模块级可变对象，用例里的恶意修改会**污染**
#   同一 pytest 进程里的其它用例 ⇒ 每个用例后把值原样还原。
@pytest.fixture(autouse=True)
def _restore_builtin_preset_values():
    snapshot = copy.deepcopy(list(preset_manager._BUILTIN_PRESETS))
    yield
    for original, current in zip(snapshot, list(preset_manager._BUILTIN_PRESETS)):
        current.clear()
        current.update(original)


def _builtin_by_name(name):
    return [p for p in preset_manager.load_presets() if p["name"] == name][0]


# ── 出口 1：load_presets() ─────────────────────────────────────


class TestBuiltinPresetIsolation:
    """调用方拿到的必须是**副本**"""

    def test_load_presets_result_does_not_alias_module_dict(self, tmp_config_dir):
        """★ 核心断言：改调用方拿到的字典，改不到模块级内置预设。"""
        before = preset_manager._BUILTIN_PRESETS[0]["brightness"]

        target = _builtin_by_name("文字增强")
        target["brightness"] = 999

        assert preset_manager._BUILTIN_PRESETS[0]["brightness"] == before, (
            "load_presets() 把模块级内置预设的字典本体交给了调用方")
        # 再取一次也不能看到被改过的值
        assert _builtin_by_name("文字增强")["brightness"] == before

    def test_nested_channel_gains_is_also_copied(self, tmp_config_dir):
        """嵌套的 list（channel_gains）也必须是副本，否则照样污染全局。"""
        target = _builtin_by_name("旧文件修复")
        target["channel_gains"][0] = 42.0

        assert preset_manager._BUILTIN_PRESETS[3]["channel_gains"][0] == 0.95, (
            "嵌套列表（channel_gains）被共享了")

    def test_two_loads_return_independent_objects(self, tmp_config_dir):
        """两次 load_presets() 的结果互相独立（改一个不影响另一个）。"""
        first = preset_manager.load_presets()
        second = preset_manager.load_presets()

        assert first[0] is not second[0]
        first[0]["brightness"] = -1
        assert second[0]["brightness"] != -1

    def test_user_preset_objects_are_not_shared_across_calls(self, tmp_config_dir):
        """用户预设同样不能跨调用共享（否则改一次影响所有调用方）。"""
        preset_manager.save_user_preset({
            "name": "我的预设", "mode": "auto",
            "brightness": 10, "contrast": 20,
            "gamma": 1.0, "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        })
        first = [p for p in preset_manager.load_presets() if p["name"] == "我的预设"][0]
        first["brightness"] = 777

        second = [p for p in preset_manager.load_presets() if p["name"] == "我的预设"][0]
        assert second["brightness"] == 10

    def test_module_attribute_access_also_returns_copy(self, tmp_config_dir):
        """★ 模块属性 `_BUILTIN_PRESETS` 这个出口同样不能给出本体。"""
        snap = preset_manager._BUILTIN_PRESETS
        snap[0]["brightness"] = 999

        assert preset_manager._BUILTIN_PRESETS[0]["brightness"] == 15, (
            "模块属性 _BUILTIN_PRESETS 直接把内部字典本体交出去了")

    def test_builtin_presets_accessor_returns_copy(self, tmp_config_dir):
        """公开的"只读内置预设"出口也必须给副本。"""
        snaps = preset_manager.builtin_presets()
        snaps[0]["brightness"] = 999

        assert preset_manager.builtin_presets()[0]["brightness"] == 15

    def test_export_does_not_leak_internal_object(self, tmp_config_dir, tmp_path):
        """export_preset 走 load_presets ⇒ 导出内容不得受调用方污染影响。"""
        out = str(tmp_path / "exported.json")
        assert preset_manager.export_preset("文字增强", out) is True

        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["preset"]["brightness"] == 15


# ── 出口 2：import_preset 的类型校验 ───────────────────────────


class TestImportPresetValidation:
    """导入非对象 / 坏名字 ⇒ 明确失败，不抛异常、不写坏文件"""

    def test_import_non_dict_preset_returns_none(self, tmp_config_dir, tmp_path):
        """★ reviewer 实测的崩溃点：`preset` 是字符串 ⇒ 之前抛 AttributeError。"""
        bad = str(tmp_path / "str_preset.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "preset": "not-a-dict"}, f)

        result = preset_manager.import_preset(bad)      # 不得抛异常
        assert result is None

    def test_import_non_dict_preset_writes_nothing(self, tmp_config_dir, tmp_path):
        """失败导入**不得**往预设文件里写任何东西。"""
        bad = str(tmp_path / "str_preset.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "preset": ["a", "b"]}, f)

        preset_manager.import_preset(bad)

        assert len(preset_manager.load_presets()) == 4, "坏数据被写进了预设文件"
        path = preset_manager._presets_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                assert json.load(f) == []

    def test_import_rejects_non_string_name(self, tmp_config_dir, tmp_path):
        """名字不是非空字符串 ⇒ 失败（否则会写出一条没法用的预设）。"""
        bad = str(tmp_path / "noname.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "preset": {"name": None, "mode": "auto"}}, f)

        assert preset_manager.import_preset(bad) is None
        assert len(preset_manager.load_presets()) == 4, "写进了一条没有名字的预设"

    def test_import_rejects_non_string_mode(self, tmp_config_dir, tmp_path):
        """mode 不是字符串 ⇒ 失败。"""
        bad = str(tmp_path / "badmode.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "preset": {"name": "X", "mode": 3}}, f)

        assert preset_manager.import_preset(bad) is None
        assert len(preset_manager.load_presets()) == 4


# ── 出口 3：import_preset 必须回报成败 ─────────────────────────


class TestImportPresetReportsOutcome:
    """写盘失败必须让调用方知道（不能照样返回预设名）"""

    def test_import_returns_none_when_save_fails(self, tmp_config_dir, tmp_path):
        """★ 预设文件损坏 ⇒ save_user_preset 失败 ⇒ import 必须返回 None。"""
        path = preset_manager._presets_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json at all")

        good = str(tmp_path / "good.json")
        with open(good, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "preset": {"name": "X", "mode": "auto"}}, f)

        result = preset_manager.import_preset(good)

        assert result is None, (
            "写盘失败却返回了预设名 —— 调用方会以为导入成功")

    def test_import_does_not_overwrite_corrupt_presets_file(self, tmp_config_dir, tmp_path):
        """导入失败时不得覆盖已损坏的预设文件（原文要留着给用户看）。"""
        path = preset_manager._presets_path()
        original = "not json at all"
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

        good = str(tmp_path / "good.json")
        with open(good, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "preset": {"name": "X", "mode": "auto"}}, f)

        preset_manager.import_preset(good)

        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == original

    def test_import_valid_still_returns_name(self, tmp_config_dir, tmp_path):
        """回归：成功路径仍返回预设名（既有契约不变）。"""
        good = str(tmp_path / "good.json")
        with open(good, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "preset": {"name": "好预设", "mode": "auto"}}, f)

        assert preset_manager.import_preset(good) == "好预设"
        names = [p["name"] for p in preset_manager.load_presets()]
        assert "好预设" in names
