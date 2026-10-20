"""
曝光非法参数的回落行为 — N3
来源：核销报告 .superpowers/sdd/2026-10-20_.../verify-v2a.md §5-N3

矩阵 10.5「非法参数回落默认值」此前**没有任何用例**，实测全部抛异常：
  gamma=0.0            ⇒ ZeroDivisionError: float division by zero
  gamma=-1.0           ⇒ ZeroDivisionError: 0.0 cannot be raised to a negative power
  channel_gains=(2,.5) ⇒ ValueError: wrong number of bands
  brightness="abc"     ⇒ TypeError: unsupported operand type(s) for +: 'float' and 'str'

原则：**外部输入的非法值回落默认值；内部契约破坏仍然抛**。
"""

import json
import os

import pytest
from PIL import Image

import exposure
import save_manager


@pytest.fixture
def rgb_img():
    img = Image.new("RGB", (64, 64), (200, 100, 50))
    return img


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把缓存根目录与历史文件重定向到临时目录（不写真实用户数据）"""
    import cache_manager
    import history_manager

    cache_root = str(tmp_path / "cache")
    os.makedirs(cache_root, exist_ok=True)
    monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: cache_root)
    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: os.path.join(cache_root, "cache_config.json"))
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(cache_manager, "_active_jobs", set())
    monkeypatch.setattr(cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(cache_manager, "_cache_size_cache", None)
    hist = str(tmp_path / "history.json")
    monkeypatch.setattr(history_manager, "_history_file", lambda: hist)
    monkeypatch.setattr(history_manager, "HISTORY_FILE", None)
    return cache_root, hist


# ────────────── 引擎层：apply_exposure ──────────────

class TestApplyExposureIllegalParams:
    @pytest.mark.parametrize("gamma", [0.0, -1.0, -0.5, "abc", None, True,
                                      float("nan"), float("inf")])
    def test_illegal_gamma_falls_back_to_one(self, rgb_img, gamma):
        """非法 gamma ⇒ 回落 1.0（不抛），像素与 gamma=1.0 完全一致。"""
        out = exposure.apply_exposure(rgb_img, mode="manual", gamma=gamma)
        base = exposure.apply_exposure(rgb_img, mode="manual", gamma=1.0)
        assert out.tobytes() == base.tobytes()

    def test_legal_gamma_still_applies(self, rgb_img):
        """合法 gamma 仍然生效（别把功能一起改没了）。"""
        out = exposure.apply_exposure(rgb_img, mode="manual", gamma=2.0)
        assert out.tobytes() != rgb_img.tobytes()

    @pytest.mark.parametrize("gains", [(2.0, 0.5), (1.0, 1.0, 1.0, 1.0), (),
                                       (1.0, 1.0, "x"), "abc", 5, None])
    def test_illegal_channel_gains_fall_back(self, rgb_img, gains):
        """长度不对 / 含非数字的通道增益 ⇒ 整组回落 (1,1,1)（不抛）。"""
        out = exposure.apply_exposure(rgb_img, mode="manual", channel_gains=gains)
        base = exposure.apply_exposure(rgb_img, mode="manual")
        assert out.tobytes() == base.tobytes()

    def test_legal_channel_gains_still_applied(self, rgb_img):
        out = exposure.apply_exposure(rgb_img, mode="manual",
                                      channel_gains=(2.0, 0.5, 1.0))
        assert out.getpixel((0, 0)) == (255, 50, 50)

    @pytest.mark.parametrize("name", ["brightness", "contrast", "shadows",
                                      "highlights"])
    @pytest.mark.parametrize("value", ["abc", None, float("nan"), float("inf")])
    def test_illegal_scalar_falls_back_to_zero(self, rgb_img, name, value):
        """非数字的明暗/对比度/阴影/高光 ⇒ 回落 0（不抛）。"""
        kwargs = {"mode": "manual"}
        kwargs[name] = value
        out = exposure.apply_exposure(rgb_img, **kwargs)
        base = exposure.apply_exposure(rgb_img, mode="manual")
        assert out.tobytes() == base.tobytes()

    @pytest.mark.parametrize("value", [{}, [], object()])
    def test_wrong_container_type_still_raises(self, rgb_img, value):
        """dict / list / 任意对象 ⇒ 仍抛 TypeError（内部契约破坏，不静默吞）。"""
        with pytest.raises(TypeError):
            exposure.apply_exposure(rgb_img, mode="manual", brightness=value)

    def test_numeric_string_is_accepted(self, rgb_img):
        """数字字符串（预设 JSON 里可能是字符串）⇒ 正常转换，不是非法值。"""
        out = exposure.apply_exposure(rgb_img, mode="manual", brightness="50")
        base = exposure.apply_exposure(rgb_img, mode="manual", brightness=50)
        assert out.tobytes() == base.tobytes()

    def test_unknown_mode_returns_original(self, rgb_img):
        """未知 mode ⇒ 原样返回（不抛，也不偷偷按 manual 改像素）。"""
        out = exposure.apply_exposure(rgb_img, mode="bogus")
        assert out is rgb_img
        assert out.tobytes() == rgb_img.tobytes()

    def test_wrong_type_still_raises(self, rgb_img):
        """**类型完全不对**（dict）仍抛 TypeError —— 那是内部契约破坏，不是脏数据。"""
        with pytest.raises(TypeError):
            exposure.apply_exposure(rgb_img, mode="manual", brightness={"a": 1})

    def test_off_mode_ignores_everything(self, rgb_img):
        """mode="off" 是早退分支：参数再离谱也不该抛、也不该改像素。"""
        out = exposure.apply_exposure(rgb_img, mode="off", gamma=0.0,
                                      brightness="abc",
                                      channel_gains=(2.0, 0.5))
        assert out is rgb_img


# ────────────── 规范化层：save_manager._exposure_params ──────────────

class TestExposureParamsSanitizing:
    def test_illegal_values_do_not_raise(self):
        """净化层不能因为脏值抛异常（旧代码 float("abc") 直接炸）。"""
        params = save_manager._exposure_params({
            "mode": "manual", "brightness": "abc", "contrast": None,
            "gamma": "abc", "shadows": "x", "highlights": [],
            "channel_gains": (2.0, 0.5),
        })
        assert params is not None
        assert params["brightness"] == 0
        assert params["contrast"] == 0
        assert params["gamma"] == 1.0
        assert params["shadows"] == 0
        assert params["highlights"] == 0
        assert params["channel_gains"] == (1.0, 1.0, 1.0)

    def test_legal_values_kept(self):
        params = save_manager._exposure_params({
            "mode": "manual", "brightness": 15, "contrast": 40,
            "gamma": 0.8, "shadows": -20, "highlights": 10,
            "channel_gains": [0.95, 1.0, 1.1],
        })
        assert params["brightness"] == 15
        assert params["contrast"] == 40
        assert params["gamma"] == 0.8
        assert params["shadows"] == -20
        assert params["highlights"] == 10
        assert params["channel_gains"] == (0.95, 1.0, 1.1)

    def test_illegal_gamma_out_of_range_falls_back(self):
        assert save_manager._exposure_params(
            {"mode": "manual", "gamma": -1.0})["gamma"] == 1.0
        assert save_manager._exposure_params(
            {"mode": "manual", "gamma": 0.0})["gamma"] == 1.0

    def test_preset_json_with_illegal_values_is_survivable(self):
        """预设 JSON（用户可编辑/可导入）带非法值时不能把保存带崩。"""
        preset = json.loads(
            '{"name": "坏预设", "mode": "manual", "brightness": "abc",'
            ' "gamma": 0, "channel_gains": [1.0, 2.0]}')
        params = save_manager._exposure_params(preset)
        assert params["brightness"] == 0
        assert params["gamma"] == 1.0
        assert params["channel_gains"] == (1.0, 1.0, 1.0)
        # 引擎层再挡一次（双保险）
        img = Image.new("RGB", (16, 16), "white")
        out = exposure.apply_exposure(img, **params)
        assert out is not None


# ────────────── 端到端：一次坏参数不该让整批保存失败 ──────────────

class TestSavePagesWithIllegalExposure:
    def test_save_pages_survives_illegal_gamma(self, tmp_path, isolated):
        src = tmp_path / "src.jpg"
        Image.new("RGB", (80, 60), (120, 120, 120)).save(src, "JPEG")
        out = str(tmp_path / "HP_Scan.jpg")
        result = save_manager.save_pages(
            "job_badgamma", "jpg", out, [(str(src), 0)], [[0]],
            exposure={"mode": "manual", "gamma": 0.0})
        assert len(result) == 1
        assert result.failed_pages == 0
        assert os.path.getsize(result[0]) > 0

    def test_save_pages_survives_illegal_gains(self, tmp_path, isolated):
        src = tmp_path / "src.jpg"
        Image.new("RGB", (80, 60), (120, 120, 120)).save(src, "JPEG")
        out = str(tmp_path / "HP_Scan.jpg")
        result = save_manager.save_pages(
            "job_badgains", "jpg", out, [(str(src), 0)], [[0]],
            exposure={"mode": "manual", "channel_gains": (2.0, 0.5)})
        assert len(result) == 1
        assert result.failed_pages == 0

    def test_save_pages_survives_non_numeric_brightness(self, tmp_path, isolated):
        src = tmp_path / "src.jpg"
        Image.new("RGB", (80, 60), (120, 120, 120)).save(src, "JPEG")
        out = str(tmp_path / "HP_Scan.jpg")
        result = save_manager.save_pages(
            "job_badbright", "jpg", out, [(str(src), 0)], [[0]],
            exposure={"mode": "manual", "brightness": "abc"})
        assert len(result) == 1
        assert result.failed_pages == 0
