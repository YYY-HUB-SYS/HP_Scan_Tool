"""
批次 4 · 自动纠偏接线（B4-4）单元测试 — HP Scan Tool v4.0

覆盖三条线：
  A 协调器：逐页检测 → 写 page_meta.deskew_angle → 连续超阈值发 skew_warning
  B 落盘  ：曝光 → 手动旋转 → 自动纠偏 → A4 归一（顺序 + 阈值 + 逐字节护栏）
  D 打包  ：skew_detect 被生产代码**静态 import** ⇒ PyInstaller 会收录

★ 真实样本：`.verify_out/D1_platen/HP_Scan_20261020_210142_001.jpg`（真机件，
  2480×3507 / 300dpi / RGB）。斜页用**程序旋转**该真机件生成（不跑真机扫描）。
★ 不跑真机扫描、不读写 %APPDATA%\\HP_Scan_Tool（conftest 已把 APPDATA 重定向）。
"""

import io
import json
import os
import threading
from unittest import mock

import pytest
from PIL import Image, ImageChops, ImageStat

import cache_manager
import page_meta
import save_manager
import scan_coordinator
import skew_detect
from scan_coordinator import ScanCancelled, ScanCoordinator

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_SAMPLE = os.path.join(PROJECT_ROOT, ".verify_out", "D1_platen",
                           "HP_Scan_20261020_210142_001.jpg")


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def real_page():
    """真机扫描件（PIL 图像，模块级只读一次）"""
    if not os.path.exists(REAL_SAMPLE):
        pytest.skip(f"真机样本不存在: {REAL_SAMPLE}")
    with Image.open(REAL_SAMPLE) as im:
        im.load()
        return im.copy()


@pytest.fixture(scope="module")
def small_page(real_page):
    """真机件的半尺寸版（2480×3507 → 1240×1754）：检测/纠偏结论一致，快 4 倍"""
    return real_page.resize((real_page.width // 2, real_page.height // 2),
                            Image.LANCZOS)


@pytest.fixture
def coordinator():
    c = ScanCoordinator()
    c.cfg = {}
    return c


@pytest.fixture
def scanner():
    from escl_engine import ScannerInfo
    s = ScannerInfo(name="test", ip="1.2.3.4")
    s.escl_url = "http://1.2.3.4:80/eSCL/"
    s.has_adf = False
    return s


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    root = str(tmp_path / "cache")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_default_cache_root",
                        lambda: root)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_cache_config_path",
                        lambda: os.path.join(root, "cache_config.json"))
    monkeypatch.setattr(scan_coordinator.cache_manager, "_config_cache", None)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(scan_coordinator.cache_manager, "_active_jobs", set())
    monkeypatch.setattr(scan_coordinator.cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(scan_coordinator.cache_manager, "_cache_size_cache", None)
    return root


@pytest.fixture
def params(tmp_path):
    return {
        "resolution": 300,
        "color_mode_ui": "彩色",
        "output_format": "jpg",
        "source": "平板",
        "duplex": False,
        "output_dir": str(tmp_path),
    }


def _jpeg_bytes(img: Image.Image, quality: int = 95) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _skewed_bytes(page: Image.Image, angle: float) -> bytes:
    """把真机件程序旋转 angle 度 ⇒ 斜页字节（angle>0 = 逆时针）"""
    return _jpeg_bytes(page.rotate(angle, expand=True, fillcolor=255))


def _blank_bytes(size=(640, 480)) -> bytes:
    return _jpeg_bytes(Image.new("RGB", size, "white"))


def _store_page_meta(cache_root, job_id, page_no, cache_path, **kwargs):
    """往页元数据里塞一条（模拟 GUI 侧写入手动旋转/裁边框）"""
    store = page_meta.PageMetaStore(cache_root)
    store.set_page(job_id, page_no, page_meta.PageMeta(
        cache_path=cache_path, width=kwargs.get("width", 100),
        height=kwargs.get("height", 100),
        manual_rotation=kwargs.get("manual_rotation", 0),
        deskew_angle=kwargs.get("deskew_angle", 0.0),
        a4_normalized=kwargs.get("a4_normalized", False),
        crop_rect=kwargs.get("crop_rect"),
    ))


@pytest.fixture
def cache_files(tmp_path):
    """缓存页文件目录（**放在缓存根之外**）。

    ★ save_pages 落盘成功后会删掉该 job 的缓存目录（_finalize → remove_job），
      所以校验产物时不能再从缓存根里读原件 ⇒ 缓存件放在这里。
    ★ 页元数据仍写在 cache_root（page_meta 的契约：按 job 存）。
    """
    folder = tmp_path / "cache_pages"
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder)


# ── A：逐页检测 + 页元数据 ───────────────────────────────────────────


class TestDetectAndPageMeta:
    def test_real_skewed_page_angle_written_to_page_meta(self, coordinator,
                                                         scanner, params,
                                                         cache_root,
                                                         small_page):
        """真实斜页（真机件 + 程序旋转 3°）⇒ 角度写进 page_meta。"""
        data = _skewed_bytes(small_page, 3.0)
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(data, "jpg")):
            result = coordinator.run_scan_sync(scanner, params, job_id="job_skew")

        pages = page_meta.PageMetaStore(cache_root).get_job("job_skew")
        angle = pages["1"]["deskew_angle"]
        assert abs(angle + 3.0) <= 0.5, f"角度应接近 -3.0（施加量），实际 {angle}"
        assert pages["1"]["cache_path"] == result.cached_paths[0]
        assert pages["1"]["width"] > small_page.width   # expand=True 会放大画布

    def test_blank_page_records_zero_angle(self, coordinator, scanner, params,
                                            cache_root):
        """空白页 ⇒ deskew_angle = 0.0（不纠偏、不提醒）。"""
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(_blank_bytes(), "jpg")):
            coordinator.run_scan_sync(scanner, params, job_id="job_blank")
        pages = page_meta.PageMetaStore(cache_root).get_job("job_blank")
        assert pages["1"]["deskew_angle"] == 0.0

    def test_straight_real_page_records_zero_angle(self, coordinator, scanner,
                                                    params, cache_root,
                                                    real_page):
        """摆正的真机件（近乎空白）⇒ 检不出角度 ⇒ 0.0。"""
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(_jpeg_bytes(real_page), "jpg")):
            coordinator.run_scan_sync(scanner, params, job_id="job_straight")
        pages = page_meta.PageMetaStore(cache_root).get_job("job_straight")
        assert pages["1"]["deskew_angle"] == 0.0

    def test_cache_original_bytes_never_modified(self, coordinator, scanner,
                                                  params, cache_root,
                                                  small_page):
        """★ 护栏：检测**不旋转缓存原件**（原件必须保持可追溯）。"""
        data = _skewed_bytes(small_page, 3.0)
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(data, "jpg")):
            result = coordinator.run_scan_sync(scanner, params, job_id="job_raw")
        with open(result.cached_paths[0], "rb") as f:
            written = f.read()
        assert written == data, "缓存原件的字节被改了"

    def test_existing_manual_rotation_and_crop_not_overwritten(self, coordinator,
                                                               scanner, params,
                                                               cache_root,
                                                               small_page):
        """★ 不覆盖 GUI 已写入的 manual_rotation / crop_rect。"""
        data = _skewed_bytes(small_page, 3.0)
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(data, "jpg")):
            result = coordinator.run_scan_sync(scanner, params, job_id="job_keep")
        # 模拟 GUI 侧在扫描后写入手动旋转与裁边框
        _store_page_meta(cache_root, "job_keep", 1, result.cached_paths[0],
                         manual_rotation=90, crop_rect=(1, 2, 3, 4),
                         deskew_angle=-3.0)
        # 再补写一次元数据（模拟二次同步）⇒ 字段必须还在
        scan_coordinator._record_page_meta(
            "job_keep", 1, data, result.cached_paths[0], deskew_angle=-3.0)
        meta = page_meta.PageMetaStore(cache_root).get_job("job_keep")["1"]
        assert meta["manual_rotation"] == 90
        assert list(meta["crop_rect"]) == [1, 2, 3, 4]
        assert meta["deskew_angle"] == -3.0

    def test_multipage_each_page_gets_its_own_angle(self, coordinator, scanner,
                                                      params, cache_root,
                                                      small_page):
        """ADF 多页：每页各自的角度都写进自己的页元数据。"""
        scanner.has_adf = True
        pages_data = [(_skewed_bytes(small_page, 3.0), "jpg"),
                      (_blank_bytes(), "jpg"),
                      (_skewed_bytes(small_page, 2.0), "jpg")]
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan",
                        return_value=pages_data):
            coordinator.run_scan_sync(scanner, dict(params, source="ADF"),
                                      job_id="job_multi")
        pages = page_meta.PageMetaStore(cache_root).get_job("job_multi")
        assert sorted(pages.keys()) == ["1", "2", "3"]
        assert abs(pages["1"]["deskew_angle"] + 3.0) <= 0.5
        assert pages["2"]["deskew_angle"] == 0.0        # 空白页
        assert abs(pages["3"]["deskew_angle"] + 2.0) <= 0.5

    def test_invalid_page_bytes_do_not_break_scan(self, coordinator, scanner,
                                                   params, cache_root):
        """非图片字节 ⇒ 角度按 0 处理，扫描照常完成（不炸）。"""
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(b"\xff\xd8not-an-image", "jpg")):
            result = coordinator.run_scan_sync(scanner, params, job_id="job_bad")
        assert result.page_count == 1
        pages = page_meta.PageMetaStore(cache_root).get_job("job_bad")
        assert pages["1"]["deskew_angle"] == 0.0


class TestCancelDuringDetection:
    def test_cancel_stops_remaining_pages(self, coordinator, scanner, params,
                                          cache_root, small_page):
        """★ 取消后不再把剩余页算完（每页 0.11 秒，40 页就是 4.4 秒假死）。"""
        scanner.has_adf = True
        pages_data = [(_skewed_bytes(small_page, 3.0), "jpg")] * 5
        cancel_event = threading.Event()
        calls = {"n": 0}
        real_estimate = scan_coordinator._estimate_page_skew

        def _estimate_and_cancel(data):
            calls["n"] += 1
            if calls["n"] == 2:
                cancel_event.set()      # 第 2 页检测完就取消
            return real_estimate(data)

        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan",
                        return_value=pages_data), \
             mock.patch("scan_coordinator._estimate_page_skew",
                        side_effect=_estimate_and_cancel):
            with pytest.raises(ScanCancelled):
                coordinator.run_scan_sync(scanner, dict(params, source="ADF"),
                                          cancel_event=cancel_event,
                                          job_id="job_cancel")
        # 5 页只算了 2 页（第三次调用前就被取消拦下）
        assert calls["n"] == 2, f"取消后仍在继续检测（调用 {calls['n']} 次）"

    def test_cancel_before_detection_raises(self, coordinator, scanner, params,
                                            cache_root, small_page):
        """扫描开始就已取消 ⇒ 立刻抛 ScanCancelled（不进入检测）。"""
        cancel_event = threading.Event()
        cancel_event.set()
        with mock.patch("scan_coordinator.execute_scan",
                        return_value=(_skewed_bytes(small_page, 3.0), "jpg")), \
             mock.patch("scan_coordinator._estimate_page_skew",
                        side_effect=AssertionError("不该进入检测")):
            with pytest.raises(ScanCancelled):
                coordinator.run_scan_sync(scanner, params,
                                          cancel_event=cancel_event,
                                          job_id="job_cancel2")


class TestSkewWarningSignal:
    def test_three_consecutive_skewed_pages_emits_warning(self, coordinator,
                                                           scanner, params,
                                                           cache_root,
                                                           small_page):
        """连续 3 页超阈值 ⇒ 发 skew_warning（含 job_id/页号/角度/缓存路径）。"""
        scanner.has_adf = True
        pages_data = [(_skewed_bytes(small_page, 4.0), "jpg")] * 3
        received = []
        coordinator.skew_warning.connect(
            lambda jid, no, ang, path: received.append((jid, no, ang, path)))
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan",
                        return_value=pages_data):
            result = coordinator.run_scan_sync(scanner, dict(params, source="ADF"),
                                               job_id="job_warn")
        assert len(received) == 1, f"应恰好提醒一次，实际 {received}"
        job_id, page_no, angle, cache_path = received[0]
        assert job_id == "job_warn"
        assert page_no == 3, "提醒应指向**触发**的那一页（第 3 页）"
        assert abs(angle + 4.0) <= 0.5
        assert cache_path == result.cached_paths[2]
        assert os.path.exists(cache_path), "缓存路径必须真实存在（弹窗要预览它）"

    def test_two_pages_do_not_emit(self, coordinator, scanner, params,
                                   cache_root, small_page):
        """2 页不超阈值 ⇒ 不发信号。"""
        scanner.has_adf = True
        pages_data = [(_skewed_bytes(small_page, 4.0), "jpg")] * 2
        received = []
        coordinator.skew_warning.connect(
            lambda *args: received.append(args))
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan",
                        return_value=pages_data):
            coordinator.run_scan_sync(scanner, dict(params, source="ADF"),
                                      job_id="job_warn2")
        assert received == []

    def test_blank_pages_do_not_emit(self, coordinator, scanner, params,
                                     cache_root):
        """空白页不触发提醒。"""
        scanner.has_adf = True
        pages_data = [(_blank_bytes(), "jpg")] * 5
        received = []
        coordinator.skew_warning.connect(lambda *args: received.append(args))
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan",
                        return_value=pages_data):
            coordinator.run_scan_sync(scanner, dict(params, source="ADF"),
                                      job_id="job_warn3")
        assert received == []

    def test_streak_broken_by_straight_page(self, coordinator, scanner, params,
                                            cache_root, small_page):
        """中间断了一页 ⇒ 连续计数清零 ⇒ 3 页里凑不齐就不提醒。"""
        scanner.has_adf = True
        pages_data = [(_skewed_bytes(small_page, 4.0), "jpg"),
                      (_blank_bytes(), "jpg"),
                      (_skewed_bytes(small_page, 4.0), "jpg")]
        received = []
        coordinator.skew_warning.connect(lambda *args: received.append(args))
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan",
                        return_value=pages_data):
            coordinator.run_scan_sync(scanner, dict(params, source="ADF"),
                                      job_id="job_warn4")
        assert received == []

    def test_warning_disabled_by_config(self, coordinator, scanner, params,
                                        cache_root, small_page):
        """deskew_enabled=False ⇒ 只检测不纠偏、也不提醒。"""
        scanner.has_adf = True
        pages_data = [(_skewed_bytes(small_page, 4.0), "jpg")] * 3
        received = []
        coordinator.skew_warning.connect(lambda *args: received.append(args))
        coordinator.cfg = {"deskew_enabled": False}
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan",
                        return_value=pages_data):
            coordinator.run_scan_sync(scanner, dict(params, source="ADF"),
                                      job_id="job_warn5")
        assert received == []
        # 但角度仍然被检测并记录（"只检测不纠偏"）
        pages = page_meta.PageMetaStore(cache_root).get_job("job_warn5")
        assert abs(pages["1"]["deskew_angle"] + 4.0) <= 0.5

    def test_custom_consecutive_threshold(self, coordinator, scanner, params,
                                          cache_root, small_page):
        """skew_warn_consecutive 可配（这里设 2 ⇒ 2 页就提醒）。"""
        scanner.has_adf = True
        pages_data = [(_skewed_bytes(small_page, 4.0), "jpg")] * 2
        received = []
        coordinator.skew_warning.connect(lambda *args: received.append(args))
        coordinator.cfg = {"skew_warn_consecutive": 2, "skew_warn_threshold": 3.0}
        with mock.patch("scan_coordinator.probe_scanner_status",
                        return_value=(True, {"state": "Idle", "adf_loaded": True})), \
             mock.patch("scan_coordinator.execute_multipage_scan",
                        return_value=pages_data):
            coordinator.run_scan_sync(scanner, dict(params, source="ADF"),
                                      job_id="job_warn6")
        assert len(received) == 1
        assert received[0][1] == 2


class TestDeskewConfig:
    def test_defaults_written_into_scan_config(self, coordinator, tmp_path,
                                                monkeypatch):
        """★ 阈值写进 scan_config.json（用现有 load_config/save_config）。"""
        appdata = tmp_path / "appdata"
        appdata.mkdir(exist_ok=True)
        monkeypatch.setenv("APPDATA", str(appdata))
        cfg = coordinator.load_config()
        assert cfg["deskew_enabled"] is True
        assert cfg["deskew_apply_threshold"] == 0.5
        assert cfg["skew_warn_threshold"] == 3.0
        assert cfg["skew_warn_consecutive"] == 3
        path = appdata / "HP_Scan_Tool" / "scan_config.json"
        assert path.exists(), "阈值必须落盘"
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["deskew_apply_threshold"] == 0.5
        assert on_disk["skew_warn_consecutive"] == 3

    def test_existing_config_keys_are_preserved(self, coordinator, tmp_path,
                                                 monkeypatch):
        """已有配置里的其它键不能被默认值覆盖掉。"""
        appdata = tmp_path / "appdata"
        (appdata / "HP_Scan_Tool").mkdir(parents=True, exist_ok=True)
        (appdata / "HP_Scan_Tool" / "scan_config.json").write_text(
            json.dumps({"resolution": 600, "skew_warn_threshold": 5.0}),
            encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(appdata))
        cfg = coordinator.load_config()
        assert cfg["resolution"] == 600
        assert cfg["skew_warn_threshold"] == 5.0     # 用户改过的值不被覆盖
        assert cfg["deskew_apply_threshold"] == 0.5  # 缺的键被补上

    def test_illegal_values_fall_back_to_defaults(self, coordinator):
        """脏值（字符串/None/负数）⇒ 回落默认值，不炸。"""
        resolved = save_manager.resolve_deskew_settings({
            "deskew_enabled": "false",
            "deskew_apply_threshold": "abc",
            "skew_warn_threshold": -1,
            "skew_warn_consecutive": 0,
        })
        assert resolved["deskew_enabled"] is False
        assert resolved["deskew_apply_threshold"] == 0.5
        assert resolved["skew_warn_threshold"] == 3.0
        assert resolved["skew_warn_consecutive"] == 3


# ── B：落盘时施加纠偏 ────────────────────────────────────────────────


def _out_base(tmp_path, name):
    """落盘用的输出基名（save_pages 自己会加 _001 后缀）"""
    return str(tmp_path / name)


def _out_path(base, ext="jpg"):
    """产物实际落盘路径（save_manager 的命名规则：单组非 PDF ⇒ base_001.ext）"""
    return save_manager.build_output_name(str(base), ext, 1, 1, 1)


def _write_cache_page(cache_root, job_id, page_no, data, ext="jpg", **meta_kwargs):
    """
    写一页缓存件 + 对应的页元数据，返回缓存件路径。

    ★ 缓存件放在**缓存根之外**（缓存根的同级目录 cache_pages/）：
      save_pages 落盘成功后 _finalize 会删掉该 job 的缓存目录
      （cache_manager.remove_job），校验产物时不能再从缓存根里读原件。
    ★ 页元数据仍写在 cache_root（page_meta 的契约：按 job 存）。
    """
    parent = os.path.dirname(os.path.abspath(cache_root))
    folder = os.path.join(parent, "cache_pages", job_id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"page_{page_no:03d}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
    _store_page_meta(cache_root, job_id, page_no, path, width=width,
                     height=height, **meta_kwargs)
    return path


class TestApplyDeskewOnSave:
    def test_skewed_output_is_straighter_than_without(self, tmp_path,
                                                       cache_root, small_page):
        """★ 斜页落盘 ⇒ 产物比不纠偏更正。"""
        job = "job_apply"
        data = _skewed_bytes(small_page, 3.0)
        cache_file = _write_cache_page(cache_root, job, 1, data,
                                       deskew_angle=-3.0)

        out_skewed = _out_base(tmp_path, "skewed")
        out_plain = _out_base(tmp_path, "plain")
        save_manager.save_pages(job, "jpg", out_skewed,
                                [(cache_file, 0)], [[0]])
        # 不纠偏的对照：把角度抹掉再存一遍
        _store_page_meta(cache_root, job, 1, cache_file,
                         width=small_page.width, height=small_page.height,
                         deskew_angle=0.0)
        save_manager.save_pages(job, "jpg", out_plain,
                                [(cache_file, 0)], [[0]])

        with Image.open(_out_path(out_skewed, "jpg")) as im:
            skew_angle = skew_detect.estimate_skew(im)
        with Image.open(_out_path(out_plain, "jpg")) as im:
            plain_angle = skew_detect.estimate_skew(im)
        assert plain_angle is not None and abs(plain_angle + 3.0) <= 0.6, \
            f"对照产物应仍是斜的，实际 {plain_angle}"
        assert skew_angle is None or abs(skew_angle) <= 0.5, \
            f"纠偏后应接近 0 度，实际 {skew_angle}"

    def test_straight_page_output_byte_identical(self, tmp_path, cache_root,
                                                  small_page):
        """★ 直页（角度 0）落盘 ⇒ 与不纠偏**逐字节相同**。"""
        job = "job_straight_save"
        data = _jpeg_bytes(small_page)
        cache_file = _write_cache_page(cache_root, job, 1, data,
                                       deskew_angle=0.0)

        out = _out_base(tmp_path, "a")
        save_manager.save_pages(job, "jpg", out, [(cache_file, 0)], [[0]])
        # 参照物：改造前的行为（直接复制缓存字节）
        import shutil
        reference = _out_base(tmp_path, "ref")
        shutil.copy2(cache_file, reference)
        assert open(_out_path(out, "jpg"), "rb").read() == \
            open(reference, "rb").read(), \
            "角度为 0 时产物必须与不纠偏逐字节相同"

    def test_exposure_off_still_byte_identical(self, tmp_path, cache_root,
                                               small_page):
        """★ 既有护栏：曝光 off ⇒ 产物与改造前逐字节一致（纠偏不介入）。"""
        job = "job_exposure"
        data = _jpeg_bytes(small_page)
        cache_file = _write_cache_page(cache_root, job, 1, data,
                                       deskew_angle=0.0)

        out_none = _out_base(tmp_path, "none")
        out_off = _out_base(tmp_path, "off")
        save_manager.save_pages(job, "jpg", out_none, [(cache_file, 0)], [[0]])
        save_manager.save_pages(job, "jpg", out_off, [(cache_file, 0)], [[0]],
                                exposure={"mode": "off"})
        assert open(_out_path(out_none, "jpg"), "rb").read() == \
            open(_out_path(out_off, "jpg"), "rb").read()

    def test_below_threshold_not_applied(self, tmp_path, cache_root,
                                          small_page):
        """低于施加阈值（0.5°）⇒ 不施加（像素一个都不动）。"""
        job = "job_thresh"
        data = _jpeg_bytes(small_page)
        cache_file = _write_cache_page(cache_root, job, 1, data,
                                       deskew_angle=0.3)

        out = _out_base(tmp_path, "thresh")
        save_manager.save_pages(job, "jpg", out, [(cache_file, 0)], [[0]])
        assert open(_out_path(out, "jpg"), "rb").read() == \
            open(cache_file, "rb").read(), \
            "0.3° < 施加阈值 0.5° ⇒ 必须逐字节不变（走快路径）"

    def test_threshold_helper_semantics(self):
        """★ 两个阈值的语义：施加阈值 vs 检出下限。"""
        # 施加：低于阈值不施加
        assert save_manager.deskew_angle_to_apply(0.3, 0.5) == 0.0
        assert save_manager.deskew_angle_to_apply(0.5, 0.5) == 0.5
        assert save_manager.deskew_angle_to_apply(-3.0, 0.5) == -3.0
        assert save_manager.deskew_angle_to_apply(0.0, 0.5) == 0.0
        assert save_manager.deskew_angle_to_apply(None, 0.5) == 0.0
        # 检出下限在 skew_detect 内部：低于 0.75° 根本测不出来
        assert skew_detect._MIN_EFFECTIVE_ANGLE == 0.75
        # 默认施加阈值(0.5) < 检出下限(0.75) ⇒ 默认下施加阈值永不生效
        assert (save_manager.DESKEW_CONFIG_DEFAULTS["deskew_apply_threshold"]
                < skew_detect._MIN_EFFECTIVE_ANGLE)

    def test_deskew_disabled_leaves_output_untouched(self, tmp_path,
                                                      cache_root, small_page,
                                                      monkeypatch):
        """deskew_enabled=False ⇒ 一页都不纠偏。"""
        job = "job_disabled"
        data = _skewed_bytes(small_page, 3.0)
        cache_file = _write_cache_page(cache_root, job, 1, data,
                                       deskew_angle=-3.0)
        monkeypatch.setattr(save_manager, "load_deskew_settings",
                            lambda: save_manager.resolve_deskew_settings(
                                {"deskew_enabled": False}))
        out = _out_base(tmp_path, "off")
        save_manager.save_pages(job, "jpg", out, [(cache_file, 0)], [[0]])
        assert open(_out_path(out, "jpg"), "rb").read() == \
            open(cache_file, "rb").read()

    def test_manual_rotation_and_deskew_both_applied(self, tmp_path,
                                                      cache_root, small_page):
        """★ 手动旋转（90° 倍数）与自动纠偏**两者都要**（先转手动、再纠偏）。

        判据：产物与"先转手动、再纠偏"手工复算的结果**逐像素一致**
        （JPEG 有损 ⇒ 用平均绝对差判定，容差远小于"少纠偏一次"的差距）。
        """
        job = "job_both"
        data = _skewed_bytes(small_page, 3.0)
        cache_file = _write_cache_page(cache_root, job, 1, data,
                                       deskew_angle=-3.0)
        out = _out_base(tmp_path, "both")
        save_manager.save_pages(job, "jpg", out, [(cache_file, 90)], [[0]])

        with Image.open(cache_file) as src:
            src.load()
            expected = src.rotate(90, expand=True).rotate(
                -3.0, resample=Image.BICUBIC, fillcolor=255)
            with Image.open(_out_path(out, "jpg")) as res:
                res.load()
                assert res.width > res.height, "手动旋转没有生效"
                diff = ImageChops.difference(res.convert("RGB"),
                                             expected.convert("RGB"))
                mad = ImageStat.Stat(diff).mean[0]
                # 只转手动、不纠偏的对照：差距必须大得多
                only_rotate = src.rotate(90, expand=True)
                diff2 = ImageChops.difference(res.convert("RGB"),
                                              only_rotate.convert("RGB"))
                mad2 = ImageStat.Stat(diff2).mean[0]
        assert mad < 6.0, f"产物与[先转手动、再纠偏]的结果不符（平均差 {mad}）"
        assert mad2 > mad * 2, f"纠偏没有生效（{mad} vs {mad2}）"

    def test_order_exposure_rotate_deskew_a4(self, tmp_path, cache_root):
        """★ 组合顺序：曝光 → 手动旋转 → 自动纠偏 → A4 归一。

        用一张**带标记**的构造图验顺序（PNG 无损 ⇒ 可逐像素比对）：
          - 手动旋转 90°（expand=True）后画布从 400×300 变成 300×400；
          - 随后的自动纠偏（小角度、不 expand）**不改变画布尺寸**；
          - 最终产物必须与"先转手动、再纠偏"手工复算的结果**逐像素相同**。
        """
        job = "job_order"
        # 构造图：左上角一个黑块（标记）+ 若干横向文本行
        img = Image.new("L", (400, 300), 255)
        for y in range(40, 260, 20):
            for x in range(60, 340, 8):
                img.putpixel((x, y), 20)
        for y in range(10, 40):
            for x in range(10, 40):
                img.putpixel((x, y), 0)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        cache_file = _write_cache_page(cache_root, job, 1, buf.getvalue(),
                                       ext="png", deskew_angle=-3.0)

        out = _out_base(tmp_path, "order")
        save_manager.save_pages(job, "png", out, [(cache_file, 90)], [[0]])

        expected = img.rotate(90, expand=True).rotate(
            -3.0, resample=Image.BICUBIC, fillcolor=255)
        with Image.open(_out_path(out, "png")) as res:
            res.load()
            assert res.size == (300, 400), \
                f"画布尺寸不对（手动旋转 + 纠偏后应为 300×400）：{res.size}"
            assert res.tobytes() == expected.tobytes(), \
                "产物与'先转手动、再纠偏'的结果不一致（顺序错了）"
            # 标记（黑块）跟着旋转走了：深色像素仍然在
            hist = res.convert("L").histogram()
            assert sum(hist[:40]) > 0, "标记丢了"

    def test_grayscale_rgb_and_dpi_variants_do_not_crash(self, tmp_path,
                                                          cache_root,
                                                          small_page):
        """灰度页 / RGB 页 / 不同 DPI 都不炸。"""
        job = "job_variants"
        variants = {
            "gray.jpg": small_page.convert("L"),
            "rgb.jpg": small_page.convert("RGB"),
        }
        for name, img in variants.items():
            data = _jpeg_bytes(img)
            cache_file = _write_cache_page(cache_root, job, 1, data,
                                           deskew_angle=-3.0)
            out = _out_base(tmp_path, name.rsplit(".", 1)[0])
            save_manager.save_pages(job, "jpg", out, [(cache_file, 0)], [[0]])
            assert os.path.getsize(_out_path(out, "jpg")) > 0

    def test_pdf_path_applies_deskew(self, tmp_path, cache_root, small_page):
        """PDF 落盘路径同样施加纠偏。"""
        job = "job_pdf"
        data = _skewed_bytes(small_page, 3.0)
        cache_file = _write_cache_page(cache_root, job, 1, data,
                                       deskew_angle=-3.0)
        out = _out_base(tmp_path, "out")
        save_manager.save_pages(job, "pdf", out, [(cache_file, 0)], [[0]])
        assert os.path.exists(_out_path(out, "pdf"))
        assert os.path.getsize(_out_path(out, "pdf")) > 0

    def test_save_single_applies_deskew(self, tmp_path, cache_root, small_page):
        """单页保存路径同样施加纠偏。"""
        job = "job_single"
        data = _skewed_bytes(small_page, 3.0)
        cache_file = _write_cache_page(cache_root, job, 1, data,
                                       deskew_angle=-3.0)
        out = _out_base(tmp_path, "single")
        save_manager.save_single(cache_file, "jpg", out, job_id=job)
        with Image.open(out + ".jpg") as im:
            angle = skew_detect.estimate_skew(im)
        assert angle is None or abs(angle) <= 0.6, f"单页保存没纠偏（{angle}）"

    def test_deskew_keeps_canvas_size(self, tmp_path, cache_root, small_page):
        """不 expand ⇒ 画布尺寸不变（只多了一圈补白）。"""
        job = "job_size"
        data = _skewed_bytes(small_page, 3.0)
        cache_file = _write_cache_page(cache_root, job, 1, data,
                                       deskew_angle=-3.0)
        out = _out_base(tmp_path, "size")
        save_manager.save_pages(job, "jpg", out, [(cache_file, 0)], [[0]])
        with Image.open(cache_file) as src, \
                Image.open(_out_path(out, "jpg")) as res:
            assert res.size == src.size, \
                f"画布尺寸变了：{src.size} → {res.size}"

    def test_600dpi_source_not_cropped(self, tmp_path):
        """★ 既有护栏：600 DPI 源图不被裁切（纠偏在 A4 归一之前，顺序不变）。"""
        src = Image.new("RGB", (4960, 7015), "white")
        canvas = save_manager._a4_canvas(src, dpi=300)
        try:
            assert canvas.size == (2480, 3507)
        finally:
            canvas.close()
            src.close()


class TestDeskewAngleResolution:
    def test_angle_resolved_by_cache_path(self, cache_root):
        by_page, by_path = save_manager.load_job_deskew_angles("")
        assert by_page == {} and by_path == {}

    def test_angle_resolved_by_page_number_from_filename(self, cache_root):
        job = "job_resolve"
        store = page_meta.PageMetaStore(cache_root)
        store.set_page(job, 3, page_meta.PageMeta(
            cache_path=os.path.join(cache_root, job, "page_003.jpg"),
            width=10, height=10, deskew_angle=-2.5))
        angles = save_manager.resolve_deskew_angles(
            job, [(os.path.join(cache_root, job, "crop_page_003.jpg"), 0)])
        assert angles == [-2.5]

    def test_angle_resolved_by_index_fallback(self, cache_root):
        job = "job_index"
        store = page_meta.PageMetaStore(cache_root)
        store.set_page(job, 1, page_meta.PageMeta(
            cache_path=os.path.join(cache_root, job, "page_001.jpg"),
            width=10, height=10, deskew_angle=-1.5))
        angles = save_manager.resolve_deskew_angles(job, [("whatever.jpg", 0)])
        assert angles == [-1.5]


# ── D：skew_detect 会被 PyInstaller 收录 ────────────────────────────


class TestSkewDetectIsLinked:
    def test_production_code_statically_imports_skew_detect(self):
        """★ skew_detect 必须被**生产代码静态 import** ⇒ 才会进 EXE。"""
        for name in ("scan_coordinator.py", "save_manager.py"):
            src = open(os.path.join(PROJECT_ROOT, name), encoding="utf-8").read()
            assert "import skew_detect" in src, \
                f"{name} 没有静态 import skew_detect ⇒ 不会进 EXE"

    def test_skew_detect_is_imported_at_runtime(self):
        import sys
        import importlib
        importlib.import_module("scan_coordinator")
        importlib.import_module("save_manager")
        assert "skew_detect" in sys.modules

    def test_skew_detect_has_no_third_party_dependency(self):
        """★ 不新增任何第三方包：skew_detect 只依赖 PIL。"""
        src = open(os.path.join(PROJECT_ROOT, "skew_detect.py"),
                   encoding="utf-8").read()
        assert "from PIL import Image" in src
        for forbidden in ("import numpy", "import cv2", "import deskew",
                          "import scipy"):
            assert forbidden not in src, f"skew_detect 不得依赖 {forbidden}"
