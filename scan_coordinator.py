"""
扫描编排器 — HP Scan Tool v4.0
从 hp_scan_gui 提取的业务逻辑层，不依赖 tkinter。
负责: 配置持久化 / 扫描仪发现与探活 / 扫描执行与缓存 / 保存确认与历史。

自动纠偏（批次 4）：
- 每页写入缓存之后调用 skew_detect.estimate_skew 测一次倾斜角，
  写进 page_meta.deskew_angle（**绝不旋转缓存原件**）
- 连续 N 页角度超阈值 ⇒ 发 skew_warning 信号（弹窗由 GUI 侧接线）
- 落盘时的实际纠偏由 save_manager 施加（曝光 → 旋转 → 纠偏 → A4 归一）
"""

import copy
import io
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime

from PIL import Image
from PySide6.QtCore import QObject, QMutex, QMutexLocker, QRunnable, QThreadPool, Signal

import cache_manager
import config_io
import history_manager
import page_meta
import skew_detect
from escl_engine import (
    probe_escl, fetch_capabilities,
    probe_scanner_status, execute_scan, execute_multipage_scan,
    ScannerInfo,
    ScanCancelled as EngineScanCancelled,
)
from save_manager import (DESKEW_CONFIG_DEFAULTS, resolve_deskew_settings)
from wsd_engine import discover_all_scanners

logger = logging.getLogger(__name__)

# 颜色模式 UI 文本 → 协议值
COLOR_MODE_MAP = {
    "彩色": "RGB24",
    "灰度": "Grayscale8",
    "黑白": "BlackAndWhite1",
}

# 来源 UI 文本 → 协议值
SOURCE_MAP = {
    "平板": "Platen",
    "ADF": "Feeder",
}

# 默认输出目录
DEFAULT_OUT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "HP_Scans")

# ── 用户可见提示文案（F-3）──
# ① 能力查询**失败/超时**（capabilities_known=False）⇒ 能力未知 ⇒ 按用户
#    显式选择走（绝不静默改成平板），并告诉用户下一步该做什么。
WARN_CAPABILITY_QUERY_FAILED = (
    "未能确认扫描仪的输稿器（ADF）能力（能力查询失败或超时），"
    "已按您选择的「输稿器」继续扫描；若设备实际没有输稿器，扫描会直接报错。"
    "可稍后重试，或把来源改回「平板」后重扫。")
# 向后兼容别名：旧名保留，指向同一个常量（既有调用点/用例不受影响）
WARN_CAPABILITY_UNKNOWN = WARN_CAPABILITY_QUERY_FAILED
# ② 能力查询**成功**且解析结果里没有 Adf 段 ⇒ 设备确实没有输稿器
#    ⇒ 已回退平板，提示用户"设备不支持输稿器"。
WARN_NO_ADF = "已确认该扫描仪不支持输稿器（ADF），已改为平板（Platen）扫描。"
WARN_NO_PAPER = "输稿器（ADF）里没有纸，已改为平板（Platen）扫描。"
WARN_PROBE_FAILED = "输稿器（ADF）状态探测失败，已按您选择的「输稿器」继续扫描。"

# 参与"能力是否真的拿到"判定的字段（只看能力，不看身份字段）
_CAPABILITY_FIELDS = ("has_adf", "has_duplex", "has_platen", "max_width",
                      "max_height", "resolutions", "color_modes", "formats",
                      "model", "serial")


def capabilities_known(scanner) -> bool:
    """
    该扫描仪的 eSCL 能力是否**已经确认拿到**。

    数据来源：`escl_engine.ScannerInfo.capabilities_known` 字段 —— 由
    `fetch_capabilities` 在「HTTP 200 + XML 可解析」时就地置 True；
    失败 / 超时 / 畸形 XML ⇒ 保持 False。
    （用 `getattr` 兜底，兼容还没加该字段的旧对象/替身。）
    """
    return bool(getattr(scanner, "capabilities_known", False))


def mark_capabilities_known(scanner, known: bool = True) -> None:
    """
    标记该扫描仪的 eSCL 能力是否已确认拿到。

    `escl_engine.ScannerInfo` 现在已有同名**正式字段**（2026-10-21 落地），
    本函数只是它的薄封装（保留是为了既有调用点与用例不必改）。
    """
    try:
        setattr(scanner, "capabilities_known", bool(known))
    except Exception as e:  # pragma: no cover - 极端情况：对象不允许加属性
        logger.debug("标记扫描仪能力状态失败: %s", e)


def _capability_fingerprint(scanner) -> tuple:
    """能力字段指纹（list 转 tuple 便于比较）"""
    values = []
    for name in _CAPABILITY_FIELDS:
        v = getattr(scanner, name, None)
        if isinstance(v, list):
            v = tuple(v)
        values.append(v)
    return tuple(values)


def fetch_capabilities_tracked(scanner, timeout: float = 3.0):
    """
    调用 escl_engine.fetch_capabilities，并判定"能力是否真的拿到了"。

    ★ **主判据 = `scanner.capabilities_known`**（终局方案，2026-10-21 落地）
      `fetch_capabilities` 在「HTTP 200 + XML 可解析」时把该字段置 True；
      失败 / 超时 / 畸形 XML 一律不置位。

    为什么不再用"能力字段指纹前后对比"当主判据：那条启发式在真实链路上有
    **3 条独立失效路径，其中 2 条是系统性的** ——
      ① WSD 发现链路：`wsd_engine` 已经先查过一次能力 ⇒ 本次查询是**幂等
         no-op** ⇒ 指纹前后完全相同 ⇒ 永远判"能力未知"；
      ② GUI 探测链路：GUI 直接调 `fetch_capabilities` ⇒ 协调器看不到任何
         字段变化 ⇒ 永远判"能力未知"；
      ③ 能力字段恰好等于 `ScannerInfo` 默认值（max_width=2480 / max_height=3508
         / resolutions=[75,150,200,300,600] / color_modes=[...] / formats=[...]
         / model='' / serial=''）⇒ 成功查询被误判为"未知"。

    后果：真·无 ADF 的设备被判成"能力未知" ⇒ 用户选「输稿器」时**不回退
    平板** ⇒ 设备回 503 ⇒ 用户看到扫描失败而不是自动落平板。

    ★ **指纹对比保留为兜底**（残余作用）：仅当本次调用确实改动了能力字段、
    但调用方（如被 mock 掉的 `fetch_capabilities`）没有置位时，补判 True。
    指纹"不变 ⇒ 未知"这条推断**不再使用**（它是不安全的方向）。
    """
    before = _capability_fingerprint(scanner)
    try:
        result = fetch_capabilities(scanner, timeout=timeout)
    except Exception as e:
        logger.debug("能力查询异常: %s", e)
        result = scanner
    if capabilities_known(result):
        return result
    # 兜底：字段确实变了 ⇒ 能力文档确实拿到了（调用方没置位时补判）
    if _capability_fingerprint(result) != before:
        mark_capabilities_known(result, True)
    return result


class ScanCancelled(EngineScanCancelled):
    """扫描被用户取消（协调器侧抛出的那一个）。

    ★ **必须继承引擎侧的 `escl_engine.ScanCancelled`**：
    eSCL / 监听 / WIA 三条引擎路径抛的都是**引擎那个类**
    （`wia_engine` 明确 `from escl_engine import ScanCancelled`），
    而这里原来自己定义了一个**互不相关**的 `class ScanCancelled(Exception)`
    ⇒ 引擎抛的取消会越过 `except ScanCancelled` 直落 `except Exception`
    ⇒ **用户点取消却被报成"扫描失败"**（发 `scan_failed` 而不是 `scan_cancelled`）。

    现在本类是引擎那个类的**子类**，因此捕获**基类**即可同时接住：
      ① 引擎抛的 `escl_engine.ScanCancelled`（及其子类 `ScanJobCancelled`）
      ② 协调器自己抛的本类
    """

    def __init__(self, job_id: str = ""):
        super().__init__(f"扫描已取消: {job_id}")
        self.job_id = job_id


@dataclass
class ScanResult:
    """一次扫描的结果"""
    job_id: str
    ext: str
    page_count: int
    cached_paths: list
    output_path: str
    source: str
    adf_detected: bool


def _label_for(s: ScannerInfo) -> str:
    """获取扫描仪显示名称"""
    if s.custom_name:
        return s.custom_name
    return s.display_name


def _ip_label_for(s: ScannerInfo) -> str:
    """获取扫描仪 IP 和型号信息"""
    parts = []
    if s.ip:
        parts.append(s.ip)
    if s.model:
        parts.append(s.model)
    return " | ".join(parts) if parts else "未知设备"


def _app_dir():
    """配置目录：%APPDATA%\\HP_Scan_Tool\\（Windows 标准应用数据目录）"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        # 回退到用户主目录
        appdata = os.path.expanduser("~")
    path = os.path.join(appdata, "HP_Scan_Tool")
    os.makedirs(path, exist_ok=True)
    return path


def load_config() -> dict:
    """读取 scan_config.json"""
    path = os.path.join(_app_dir(), "scan_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def apply_deskew_defaults(cfg: dict) -> bool:
    """
    把纠偏阈值的缺省值补进配置字典（就地修改）。返回**是否真的补了键**。

    ★ 补了键才值得落盘（scan_config.json）：
      deskew_enabled / deskew_apply_threshold / skew_warn_threshold /
      skew_warn_consecutive —— 用户可在 GUI 里改，改完由 GUI 写回同一个文件。
    ★ 默认值本体在 save_manager.DESKEW_CONFIG_DEFAULTS（唯一真源），
      协调器与落盘层共用同一份语义，不会两处漂移。
    """
    if not isinstance(cfg, dict):
        return False
    changed = False
    for key, default in DESKEW_CONFIG_DEFAULTS.items():
        if key not in cfg:
            cfg[key] = default
            changed = True
    return changed


def save_config(cfg: dict) -> bool:
    """
    原子写入 scan_config.json。

    与 cache/history/profile 三家一致，收口到 config_io：
      - atomic_write_json：mkstemp 唯一临时名 + 写后回读校验 + os.replace
        （旧的固定临时名 `scan_config.json.tmp` 在并发写下会交错写，
          产出**非法 JSON** 并被 os.replace 发布成正式配置 ⇒
          下次 load_config 命中 JSONDecodeError 返回 {} ⇒ 用户设置整体丢失）
      - config_write_lock：同目录 <name>.lock 跨进程排他锁（**严格版**）

    返回是否真的写入：
    - True  = 已落盘
    - False = 拿不到跨进程锁（本次**未写入**，原文件保持不变）或写失败
    失败**不抛异常**（避免 GUI 槽里未捕获异常），但必须**记 error 日志** ——
    写失败绝不能只留一条 debug 日志（用户会以为设置已保存）。
    """
    path = os.path.join(_app_dir(), "scan_config.json")
    try:
        with config_io.config_write_lock(path):
            config_io.atomic_write_json(path, cfg)
    except config_io.ConfigLockTimeout as e:
        logger.error("配置保存被跳过（拿不到跨进程锁，本次未写入）: %s（%s）", path, e)
        return False
    except Exception as e:
        logger.error("配置保存失败: %s", e)
        return False
    return True


class ScanCoordinator(QObject):
    """
    扫描编排器 — 事件驱动。
    持有扫描线程池，通过信号向 GUI 报告进度与结果。
    GUI 只负责收集参数与呈现结果，不直接调用引擎。
    """

    scan_started = Signal(str)
    scan_progress = Signal(str, str, int)
    scan_finished = Signal(str, object)
    scan_failed = Signal(str, str)
    scan_cancelled = Signal(str)
    # F-3：来源决策的**用户可见提示**（能力未确认 / 被回退到平板等）。
    # GUI 侧接线：连到状态栏或 QMessageBox（接线点在片 3 修复波次）。
    source_warning = Signal(str)
    # ★ 批次 4：倾斜强提醒（自动纠偏接线）。
    #   签名：skew_warning(job_id: str, page_no: int, angle: float, cache_path: str)
    #     - job_id     ：该页所属的扫描任务 id
    #     - page_no    ：页号（1 基，与 page_meta 的页号一致）
    #     - angle      ：该页的纠偏角（度，PIL 角度语义，可直接施加）
    #     - cache_path ：该页的缓存原件路径（弹窗预览图从这里离屏加载）
    #   触发条件：**连续 N 页**（skew_warn_consecutive，默认 3）
    #             角度绝对值 > skew_warn_threshold（默认 3.0°）。
    #   ★ 在**工作线程**发出 ⇒ GUI 侧必须走现有的 `_UiPostMixin._post_ui`
    #     投递到主线程，**不要**在工作线程里直接弹窗。
    skew_warning = Signal(str, int, float, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scanners: list[ScannerInfo] = []
        self._scanners_lock = threading.Lock()
        self.cfg: dict = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._cancel_events: dict = {}
        self._cancel_mutex = QMutex()

    # ── 配置 ──

    def load_config(self):
        cfg = load_config()
        changed = apply_deskew_defaults(cfg)
        self.cfg = cfg
        if changed:
            # 纠偏阈值写进 scan_config.json（缺省值落盘，用户可在 GUI 里改）
            self.save_config()
        return self.cfg

    def save_config(self):
        """保存配置。返回是否真的写入（False = 本次未存上）。"""
        return save_config(self.cfg)

    def deskew_settings(self) -> dict:
        """
        取纠偏相关阈值（deskew_enabled / deskew_apply_threshold /
        skew_warn_threshold / skew_warn_consecutive）。

        优先用内存里的 self.cfg（GUI 启动时 load_config 读进来、用户改完也写回）；
        self.cfg 为空时读一次配置文件。缺省/非法值一律回落默认值。
        """
        cfg = self.cfg if isinstance(self.cfg, dict) and self.cfg else load_config()
        return resolve_deskew_settings(cfg)

    def set_custom_name(self, ip: str, name: str):
        """设置设备自定义名称"""
        nicknames = self.cfg.get("nicknames", {})
        nicknames[ip] = name
        self.cfg["nicknames"] = nicknames
        self.save_config()
        # 更新内存中的扫描仪
        for s in self.scanners:
            if s.ip == ip:
                s.custom_name = name
                break

    # ── 扫描仪管理 ──

    def restore_saved_scanners(self) -> list[ScannerInfo]:
        """从配置恢复已保存的扫描仪列表"""
        saved_ips = self.cfg.get("saved_ips", [])
        nicknames = self.cfg.get("nicknames", {})
        result = []
        for item in saved_ips:
            if not isinstance(item, dict):
                continue
            ip = item.get("ip")
            if not ip:
                continue
            si = ScannerInfo(
                name=item.get("model", "Unknown"),
                ip=ip,
                model=item.get("model", ""),
            )
            si.escl_url = ""
            si.custom_name = nicknames.get(ip, "")
            result.append(si)
        with self._scanners_lock:
            self.scanners = result
        return list(result)

    def probe_scanners_background(self, scanners: list[ScannerInfo],
                                   on_complete):
        """后台探活扫描仪列表，完成后在主线程调用 on_complete(results_dict)"""
        def _probe():
            results = {}
            for s in scanners:
                url = probe_escl(s.ip, port=s.port, timeout=3.0)
                if url:
                    new_s = copy.copy(s)
                    new_s.escl_url = url
                    new_s = fetch_capabilities_tracked(new_s, timeout=3.0)
                    results[s.ip] = new_s
            on_complete(results)
        threading.Thread(target=_probe, daemon=True).start()

    def apply_probe_results(self, results: dict):
        """原子替换探活结果到 scanners 列表"""
        with self._scanners_lock:
            for i, s in enumerate(self.scanners):
                if s.ip in results:
                    self.scanners[i] = results[s.ip]

    def discover_scanners_background(self, on_complete):
        """后台发现局域网扫描仪，完成后调用 on_complete(new_count, new_scanners)"""
        def _discover():
            logger.info("发现线程启动")
            try:
                discovered = discover_all_scanners(timeout=3.0)
                logger.info(f"discover_all_scanners 返回 {len(discovered)} 台")
                for d in discovered:
                    logger.info(f"  发现: {d.name} ({d.ip})")
            except Exception as e:
                logger.error(f"discover_all_scanners 异常: {e}", exc_info=True)
                discovered = []
            with self._scanners_lock:
                existing_ips = {s.ip for s in self.scanners if s.ip}
            new_scanners = []
            
            # 并行探活所有发现的打印机
            def _probe_one(d):
                if d.ip in existing_ips:
                    return None
                try:
                    d.escl_url = probe_escl(d.ip, timeout=2.0) or ""
                    logger.info(f"  probe_escl({d.ip}) -> {d.escl_url or '失败'}")
                    if d.escl_url:
                        d = fetch_capabilities_tracked(d, timeout=2.0)
                    return d
                except Exception as e:
                    logger.error(f"  探活失败 {d.ip}: {e}")
                    return d
            
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(_probe_one, d) for d in discovered]
                for future in concurrent.futures.as_completed(futures, timeout=15):
                    result = future.result()
                    if result is not None:
                        new_scanners.append(result)
                        existing_ips.add(result.ip)

            # 添加到内存列表
            with self._scanners_lock:
                self.scanners.extend(new_scanners)
                all_for_save = list(self.scanners)

            # 持久化
            nicknames = self.cfg.get("nicknames", {})
            ips = []
            for s in all_for_save:
                if s.ip:
                    ips.append({"ip": s.ip, "model": s.model})
                    if s.custom_name:
                        nicknames[s.ip] = s.custom_name
            self.cfg["saved_ips"] = ips
            self.cfg["nicknames"] = nicknames
            self.save_config()

            on_complete(len(new_scanners), new_scanners)
        threading.Thread(target=_discover, daemon=True).start()

    def add_scanner(self, scanner: ScannerInfo):
        with self._scanners_lock:
            self.scanners.append(scanner)

    def replace_scanner(self, idx: int, scanner: ScannerInfo):
        with self._scanners_lock:
            if 0 <= idx < len(self.scanners):
                self.scanners[idx] = scanner

    # ── 扫描执行 ──

    def resolve_source(self, scanner, requested: str) -> tuple:
        """
        决定实际扫描来源，返回 (实际来源, 是否探测到输稿器有纸)。

        契约不变（两元组）。需要同时拿到"提示信息"的调用方用
        `resolve_source_detailed`。
        """
        source, adf_detected, _warning = self.resolve_source_detailed(scanner, requested)
        return source, adf_detected

    def resolve_source_detailed(self, scanner, requested: str) -> tuple:
        """
        决定实际扫描来源，返回 (实际来源, 是否探测到输稿器有纸, 提示信息或 None)。

        requested 为协议值："Platen" / "Feeder" / "Auto"
        - "Platen"：用户显式选了平板 → 保持 Platen，不探测也不覆盖
        - "Feeder"：用户选了输稿器 → 探测到有纸才用 Feeder，否则回退 Platen
        - "Auto"：探测决定，有纸用 Feeder 否则 Platen

        ★ F-3：必须区分「设备真的没有 ADF」与「我们没查到能力」。
        判据是 `scanner.capabilities_known`（由 `fetch_capabilities` 在
        「HTTP 200 + XML 可解析」时置位）：
          - **已知**（查询成功）+ 解析结果里没有 Adf 段 ⇒ 设备真的没有 ADF
            ⇒ 强制平板，提示 `WARN_NO_ADF`；
          - **未知**（查询失败/超时/畸形 XML）⇒ **保留用户显式选择的来源**
            并给出 `WARN_CAPABILITY_QUERY_FAILED` 提示；绝不静默把
            「输稿器」改成「平板」—— 用户选了输稿器却扫了平板且界面无任何
            提示，正是本轮要消灭的失败模式。

        探测失败时**保留用户请求的来源**。probe_scanner_status 显式返回
        (探测是否成功, 状态字典)："探测失败"（连接失败 / 非 200 / XML 畸形，
        ok=False）与"设备合法上报 <State>Unknown</State>"（ok=True）是两回事。
        因此判定只看 ok，**不能**用 state=="Unknown" 反推探测失败——
        否则设备真报 Unknown 时会被误判，在 Auto 模式下静默改扫平板
        （与改造前 GUI 的字符串判定不同）。若探测失败时把 Feeder 静默改成
        Platen，用户选了输稿器却扫了平板且界面无任何提示——改造前这种情形
        是会失败报错的。保留来源后，扫描本身会去暴露问题（输稿器没纸时
        扫描失败），与改造前行为一致。
        """
        if requested == "Platen":
            # 用户显式选择平板：不探测、不覆盖。改造前的 GUI 也是
            # "选平板就是平板"，这里必须保持一致，否则是行为回归。
            return "Platen", False, None

        has_adf = bool(getattr(scanner, "has_adf", False))
        escl_url = getattr(scanner, "escl_url", "")

        if not has_adf:
            if capabilities_known(scanner):
                # 能力**已确认**（查询成功且解析结果里没有 Adf 段）
                # ⇒ 设备真的没有 ADF ⇒ 强制平板。
                if requested == "Feeder":
                    return "Platen", False, WARN_NO_ADF
                return "Platen", False, None
            # 能力**未知**（查询失败/超时/XML 畸形 ⇒ capabilities_known=False）
            # ⇒ 按用户显式选择走，并把"能力未确认"提示给用户。
            if requested == "Feeder":
                return "Feeder", False, WARN_CAPABILITY_QUERY_FAILED
            # Auto：机器决策，探测不出 ADF 能力就落平板（保持既有行为）
            return "Platen", False, None

        if not escl_url:
            # 有 ADF 能力但没有 eSCL 地址：状态探测无从谈起
            #（扫描本身也会以"未配置 eSCL URL"失败，错误是显性的）。
            return "Platen", False, None

        try:
            ok, st = probe_scanner_status(escl_url, timeout=5.0)
        except Exception as e:
            # 防御性：当前实现不抛异常，但接口契约上仍可能
            logger.debug("ADF 状态探测异常，保持原来源: %s", e)
            ok, st = False, None

        if not ok:
            # 探测失败：无法判断输稿器状态，保留用户请求的来源。
            # 注意：只看 ok，不看 state=="Unknown"——设备合法上报
            # <State>Unknown</State> 时 ok=True，不算探测失败。
            if requested == "Feeder":
                return "Feeder", False, WARN_PROBE_FAILED
            return "Platen", False, None

        loaded = bool(st.get("adf_loaded"))
        if requested == "Feeder":
            if loaded:
                return "Feeder", True, None
            return "Platen", False, WARN_NO_PAPER
        # requested == "Auto"
        if loaded:
            return "Feeder", True, None
        return "Platen", False, None

    def _note_skew(self, job_id: str, page_no: int, angle: float,
                   cache_path: str, streak: int, settings: dict) -> int:
        """
        累计"连续超阈值"的页数；攒够 N 页就发一次 skew_warning。返回新的计数。

        规则（与 brief 一致）：
          - 只有 deskew_enabled=True 时才提醒（关掉总开关 ⇒ 只检测不纠偏也不提醒）
          - 角度绝对值 **>** skew_warn_threshold 才算"歪"（严格大于）
          - **连续** N 页都歪 ⇒ 发信号；中间只要有一页不歪，连续计数清零
          - 提醒后计数清零 ⇒ 每连续 N 页提醒一次，不会连着刷屏
            （GUI 侧另有"本批次不再提醒"的标志位，由 GUI 负责）
        """
        if not settings.get("deskew_enabled"):
            return 0
        if angle is None or abs(angle) <= settings.get("skew_warn_threshold", 3.0):
            return 0
        streak += 1
        if streak >= settings.get("skew_warn_consecutive", 3):
            self.skew_warning.emit(job_id, page_no, float(angle), cache_path)
            logger.warning("连续 %d 页倾斜超阈值，页 %d 角度 %.2f°",
                           streak, page_no, angle)
            return 0
        return streak

    def run_scan_sync(self, scanner, params: dict, cancel_event=None,
                      job_id: str = None) -> ScanResult:
        """
        同步执行一次扫描并写入缓存。线程池任务与单元测试都走这个方法。
        """
        resolution = params.get("resolution", 300)
        color_mode_ui = params.get("color_mode_ui", "彩色")
        output_format = params.get("output_format", "jpg")
        duplex = params.get("duplex", False)
        output_dir = params.get("output_dir") or DEFAULT_OUT_DIR
        requested = self._source_to_protocol(params.get("source", "平板"))

        source, adf_detected, warning = self.resolve_source_detailed(scanner, requested)
        if warning:
            # F-3：能力未确认 / 来源被调整等**用户可见提示**（GUI 侧接线见报告）
            logger.warning("来源提示 [%s]: %s", job_id, warning)
            self.source_warning.emit(warning)
        color_mode = COLOR_MODE_MAP.get(color_mode_ui, "RGB24")

        if job_id is None:
            job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        cache_manager.register_job(job_id)

        # job 的保护生命周期（F-4）：
        #   扫描中      → register_job（_active_jobs，受 eviction 保护）
        #   扫描成功    → register_pending_job（_pending_jobs，仍受保护）
        #   扫描失败/取消 → unregister_job（完全注销，不留受保护条目）
        #   落盘成功    → save_manager._finalize → remove_job（删目录 + 解除保护）
        #   用户放弃    → cache_manager.unregister_pending_job（GUI 侧接线）
        #
        # 为什么"扫描成功"不能立刻注销：用户此时还在预览里没点保存，
        # 可能挂几分钟；这段时间里另一条扫描把缓存顶过 95% 触发线时，
        # FIFO 会把这个 job 目录删掉 ⇒ 用户预览里的页被静默跳过
        # （产物少页 + 界面零提示）。⇒ "已扫描未保存"必须一直受保护。
        try:
            cached_paths = []
            ext = output_format

            # ── 自动纠偏（批次 4）────────────────────────────────────
            # 每页写入缓存之后检测一次倾斜角，写进 page_meta.deskew_angle；
            # 连续 N 页超阈值时发 skew_warning（弹窗由 GUI 侧接线）。
            settings = self.deskew_settings()
            skew_streak = 0

            if source == "Feeder":
                def _progress(n, _total):
                    self.scan_progress.emit(job_id, f"已扫描 {n} 页", n)

                pages = execute_multipage_scan(
                    scanner=scanner, resolution=resolution, color_mode=color_mode,
                    output_format=output_format, source=source, duplex=duplex,
                    timeout=300.0, progress_callback=_progress,
                    cancel_event=cancel_event,
                )
                if not pages:
                    # 取消时引擎会返回空页。与单页分支保持一致，抛 ScanCancelled，
                    # 让"取消"只由 scan_cancelled 信号表达，不出现"完成但 0 页"。
                    raise ScanCancelled(job_id)
                for i, (data, page_ext) in enumerate(pages, 1):
                    ext = page_ext
                    path = cache_manager.write_page(job_id, i, data, page_ext)
                    cached_paths.append(path)
                    _check_cancel(cancel_event, job_id)
                    angle = _estimate_page_skew(data)
                    _check_cancel(cancel_event, job_id)
                    _record_page_meta(job_id, i, data, path, deskew_angle=angle)
                    skew_streak = self._note_skew(job_id, i, angle, path,
                                                 skew_streak, settings)
            else:
                data, ext = execute_scan(
                    scanner=scanner, resolution=resolution, color_mode=color_mode,
                    output_format=output_format, source=source, duplex=duplex,
                    timeout=90.0, cancel_event=cancel_event,
                )
                if not data:
                    raise ScanCancelled(job_id)
                path = cache_manager.write_page(job_id, 1, data, ext)
                cached_paths.append(path)
                _check_cancel(cancel_event, job_id)
                angle = _estimate_page_skew(data)
                _check_cancel(cancel_event, job_id)
                _record_page_meta(job_id, 1, data, path, deskew_angle=angle)
                self._note_skew(job_id, 1, angle, path, skew_streak, settings)

            output_path = os.path.join(
                output_dir,
                f"HP_Scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")

            result = ScanResult(
                job_id=job_id, ext=ext, page_count=len(cached_paths),
                cached_paths=cached_paths, output_path=output_path,
                source=source, adf_detected=adf_detected,
            )
        except BaseException:
            # 扫描失败/被取消：该 job 的页元数据不再有用。
            # 此时缓存目录可能还留着（孤儿清理按"目录已不存在"判定，
            # 兜不住这种情况），必须显式清掉，否则元数据永久残留。
            _discard_page_meta(job_id)
            # 失败/取消 ⇒ 完全注销：既不留受保护条目，也不让缓存目录
            # 永久堆积（不注销的话它的缓存目录永远不会被清理）。
            cache_manager.unregister_job(job_id)
            raise

        # 扫描成功 ⇒ 转成"已扫描未保存"，继续受 eviction 保护，
        # 直到落盘成功（remove_job）或用户放弃（unregister_pending_job）。
        cache_manager.register_pending_job(job_id)
        return result

    def start_scan(self, scanner, params: dict) -> str:
        """异步启动扫描，立即返回 job_id。结果通过信号通知。"""
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        cancel_event = threading.Event()
        with QMutexLocker(self._cancel_mutex):
            self._cancel_events[job_id] = cancel_event
        self._pool.start(_ScanTask(self, scanner, params, job_id, cancel_event))
        return job_id

    def cancel_scan(self, job_id: str) -> None:
        """取消指定扫描任务"""
        with QMutexLocker(self._cancel_mutex):
            ev = self._cancel_events.get(job_id)
        if ev is not None:
            ev.set()

    def shutdown(self) -> None:
        """取消全部在途任务并停止线程池。GUI 关闭时调用。"""
        with QMutexLocker(self._cancel_mutex):
            events = list(self._cancel_events.values())
        for ev in events:
            ev.set()
        self._pool.clear()
        self._pool.waitForDone(3000)

    def _clear_cancel_event(self, job_id: str) -> None:
        with QMutexLocker(self._cancel_mutex):
            self._cancel_events.pop(job_id, None)

    @staticmethod
    def _source_to_protocol(source_text: str) -> str:
        """UI 文本 → 协议来源值（Auto 表示自动探测）"""
        if source_text == "自动":
            return "Auto"
        if source_text in SOURCE_MAP:
            return SOURCE_MAP[source_text]
        if source_text in ("Platen", "Feeder", "Auto"):
            return source_text
        return "Platen"

    # ── 缓存清理 ──

    def cleanup_stale_cache(self):
        """启动时清理过期缓存，并顺带清理孤儿页元数据"""
        cache_manager.cleanup_stale()
        self.prune_orphan_page_meta()

    def prune_orphan_page_meta(self) -> list[str]:
        """
        清理孤儿页元数据：缓存 job 目录已不存在的条目。

        启动时调用一次（不在扫描热路径上）：覆盖"缓存被 eviction、
        程序异常退出"等路径留下的残留，避免 page_meta.json 只增不减。
        """
        try:
            return page_meta.prune_orphans(cache_manager._cache_root())
        except Exception as e:
            logger.debug("清理孤儿页元数据失败: %s", e)
            return []

    # ── 持久化辅助 ──

    def persist_scan_settings(self, resolution, color_mode, output_format,
                               output_dir, source_label):
        """将当前扫描参数写入配置"""
        self.cfg.update({
            "resolution": resolution,
            "color_mode_ui": color_mode,
            "output_format": output_format,
            "output_dir": output_dir,
            "source": source_label,
        })
        self.save_config()


def _check_cancel(cancel_event, job_id: str) -> None:
    """
    取消检查：用户点了取消就立刻停（抛 ScanCancelled）。

    ★ 为什么每页都要查：倾斜检测每页约 0.11 秒，40 页的批次不查就是
      4.4 秒的"假死"（界面没反应、取消按钮按了像没按）。
      ⇒ 检测前后各查一次，最坏延迟 = 一页的检测时间。
    """
    if cancel_event is not None and cancel_event.is_set():
        raise ScanCancelled(job_id)


def _estimate_page_skew(data: bytes) -> float:
    """
    估计单页的纠偏角（度）。**无法判定时返回 0.0**（空白页/非图片/解不开）。

    ★ 角度语义与 skew_detect 一致：PIL `Image.rotate` 的角度，
      已经是**可直接施加**的纠偏量（调用方不要再取负）。
    ★ 返回 None（skew_detect 对空白页/稀疏页的判定）在这里归一为 0.0：
      0.0 = "没有需要纠正的倾斜"，既不纠偏也不提醒。
    """
    if not data:
        return 0.0
    try:
        with Image.open(io.BytesIO(data)) as img:
            angle = skew_detect.estimate_skew(img)
    except Exception as e:
        logger.debug("倾斜检测失败（按 0 度处理）: %s", e)
        return 0.0
    if angle is None:
        return 0.0
    try:
        return float(angle)
    except (TypeError, ValueError):
        return 0.0


def _record_page_meta(job_id: str, page_no: int, data: bytes, cache_path: str,
                      deskew_angle: float = 0.0) -> None:
    """
    记录单页元数据（原始尺寸 + 缓存路径 + 纠偏角）。失败不影响扫描结果。

    缓存里保留的永远是**原始扫描件**（不做任何像素改动），A4 归一与自动
    纠偏都推迟到落盘时做。原始尺寸必须在这里落盘，否则后续的自动纠偏/
    双面合并/断点续扫都没有可回溯的基准。

    ★ deskew_angle：扫描时测出来的纠偏角（度）。落盘时由 save_manager
      取回来施加；**这里绝不旋转缓存原件**（page_meta 的契约：
      cache_path 指向永不修改的原始扫描件）。
    ★ 不覆盖已有的 manual_rotation / crop_rect —— 那两个字段由 GUI 的
      预览同步写入（手动旋转 / 裁边），扫描侧补写元数据时必须保住它们。
    """
    width = height = 0
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception as e:
        logger.debug("读取页面尺寸失败 [%s]: %s", cache_path, e)
    try:
        store = page_meta.PageMetaStore(cache_manager._cache_root())
        # 保留 GUI 侧已写入的手动旋转 / 裁边框
        existing = {}
        try:
            existing = store.get_job(job_id).get(str(page_no), {}) or {}
        except Exception as e:
            logger.debug("读取既有页元数据失败 [%s]: %s", job_id, e)
            existing = {}
        crop_rect = existing.get("crop_rect")
        store.set_page(job_id, page_no, page_meta.PageMeta(
            cache_path=cache_path, width=width, height=height,
            manual_rotation=_safe_int(existing.get("manual_rotation")),
            deskew_angle=_safe_float(deskew_angle),
            a4_normalized=bool(existing.get("a4_normalized", False)),
            crop_rect=tuple(crop_rect) if crop_rect else None,
        ))
    except Exception as e:
        logger.debug("写页元数据失败 [%s]: %s", job_id, e)


def _safe_int(value, default: int = 0) -> int:
    """脏值容忍的整数转换（非法值 ⇒ 默认值，不抛）"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    """脏值容忍的浮点转换（非法值 ⇒ 默认值，不抛）"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if result != result else result   # NaN ⇒ 默认值


def _discard_page_meta(job_id: str) -> None:
    """丢弃某 job 的页元数据（失败不影响扫描结果）"""
    try:
        page_meta.PageMetaStore(cache_manager._cache_root()).remove_job(job_id)
    except Exception as e:
        logger.debug("清理页元数据失败 [%s]: %s", job_id, e)


def record_scan_history(device_name, device_ip, output_path, page_count,
                         output_format, source="gui"):
    """记录扫描历史"""
    history_manager.append({
        "device_name": device_name,
        "device_ip": device_ip,
        "page_count": page_count,
        "format": output_format,
        "file_path": output_path,
        "source": source,
    })


def cleanup_cache_job(job_id):
    """清理缓存任务"""
    cache_manager.remove_job(job_id)


def compute_page_groups(splits: list[int], total_pages: int) -> list[list[int]]:
    """根据分割点计算页面分组（纯数据逻辑）"""
    if not splits:
        return [list(range(total_pages))]
    groups = []
    prev = 0
    for sp in sorted(splits):
        if prev < sp:
            groups.append(list(range(prev, sp)))
        prev = sp
    if prev < total_pages:
        groups.append(list(range(prev, total_pages)))
    return groups


def delete_cached_page(job_id, page_idx, ext, splits):
    """删除缓存中的指定页，调整分割点。返回更新后的 splits。"""
    cache_dir = cache_manager._job_dir(job_id)
    page_path = os.path.join(cache_dir, f"page_{page_idx + 1:03d}.{ext}")
    if os.path.exists(page_path):
        os.remove(page_path)
    # 调整分割点
    new_splits = []
    for sp in splits:
        if sp < page_idx:
            new_splits.append(sp)
        elif sp > page_idx:
            new_splits.append(sp - 1)
    return new_splits


class _ScanTask(QRunnable):
    """在协调器线程池中执行的扫描任务"""

    def __init__(self, coordinator, scanner, params, job_id, cancel_event):
        super().__init__()
        self.setAutoDelete(True)
        self._coordinator = coordinator
        self._scanner = scanner
        self._params = params
        self._job_id = job_id
        self._cancel_event = cancel_event

    def run(self):
        coord = self._coordinator
        job_id = self._job_id
        try:
            coord.scan_started.emit(job_id)
            result = coord.run_scan_sync(self._scanner, self._params,
                                         cancel_event=self._cancel_event,
                                         job_id=job_id)
            if self._cancel_event.is_set():
                coord.scan_cancelled.emit(job_id)
            else:
                coord.scan_finished.emit(job_id, result)
        except EngineScanCancelled:
            # ★ 捕获**基类**（`escl_engine.ScanCancelled`），不是本模块的子类 ——
            #   引擎三条路径（eSCL / 监听 / WIA）抛的都是基类或其子类
            #   （`ScanJobCancelled`）；本模块自己的 `ScanCancelled` 是基类的子类。
            #   只捕获子类会漏掉引擎抛的那一半 ⇒ 取消被报成"扫描失败"。
            coord.scan_cancelled.emit(job_id)
        except Exception as e:
            logger.error("扫描失败 [%s]: %s", job_id, e, exc_info=True)
            coord.scan_failed.emit(job_id, str(e))
        finally:
            coord._clear_cancel_event(job_id)
