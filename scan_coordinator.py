"""
扫描编排器 — HP Scan Tool v4.0
从 hp_scan_gui 提取的业务逻辑层，不依赖 tkinter。
负责: 配置持久化 / 扫描仪发现与探活 / 扫描执行与缓存 / 保存确认与历史。
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
from escl_engine import (
    probe_escl, fetch_capabilities,
    probe_scanner_status, execute_scan, execute_multipage_scan,
    ScannerInfo,
)
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
WARN_CAPABILITY_UNKNOWN = (
    "未能确认扫描仪的输稿器（ADF）能力（能力查询失败或超时），"
    "已按您选择的「输稿器」继续扫描；若设备实际没有输稿器，扫描会直接报错。")
WARN_NO_ADF = "该扫描仪不支持输稿器（ADF），已改为平板（Platen）扫描。"
WARN_NO_PAPER = "输稿器（ADF）里没有纸，已改为平板（Platen）扫描。"
WARN_PROBE_FAILED = "输稿器（ADF）状态探测失败，已按您选择的「输稿器」继续扫描。"

# 参与"能力是否真的拿到"判定的字段（只看能力，不看身份字段）
_CAPABILITY_FIELDS = ("has_adf", "has_duplex", "has_platen", "max_width",
                      "max_height", "resolutions", "color_modes", "formats",
                      "model", "serial")


def capabilities_known(scanner) -> bool:
    """
    该扫描仪的 eSCL 能力是否**已经确认拿到**（F-3 临时判定）。

    见 `mark_capabilities_known` 的说明。
    """
    return bool(getattr(scanner, "capabilities_known", False))


def mark_capabilities_known(scanner, known: bool = True) -> None:
    """
    标记该扫描仪的 eSCL 能力是否已确认拿到。

    ★ **临时方案**（F-3）：`escl_engine.ScannerInfo` 目前没有 capabilities_known
    字段（`escl_engine.py` 不在本波次的文件所有权里），这里用**实例属性**承载。
    —— escl_engine 将来补上同名字段后，本函数与 `capabilities_known()` 的语义
    完全不变（同一个实例属性、同一个 True/False 含义），可直接平滑切换，
    协调器侧不需要再改。
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

    ★ **临时方案**（F-3）：`fetch_capabilities` 失败时只记 debug 日志并原样
    返回 scanner，调用方无法区分「设备真的没有 ADF」与「我们没查到能力」。
    这里用**能力字段指纹前后对比**判定：
      - 查询成功 ⇒ 设备至少有一项能力字段与查询前不同（型号 / 序列号 /
        分辨率 / 颜色模式 / 格式 / 最大尺寸 / 有无 ADF）⇒ 能力已知
      - 查询失败（超时 / 连不上 / XML 解析失败）⇒ 字段原封不动 ⇒ 能力未知
    判定只会"保守"地偏保守方向：能力未知时保留用户显式选择的来源，
    绝不会静默把「输稿器」改成「平板」。

    **终局方案**（推荐）：给 `ScannerInfo` 加 `capabilities_known: bool = False`
    字段，由 `fetch_capabilities` 查询成功时置 True —— 那样所有调用方
    （含 GUI / CLI / wsd_engine）自动拿到准确信号。
    """
    before = _capability_fingerprint(scanner)
    try:
        result = fetch_capabilities(scanner, timeout=timeout)
    except Exception as e:
        logger.debug("能力查询异常: %s", e)
        result = scanner
    after = _capability_fingerprint(result)
    if after != before:
        mark_capabilities_known(result, True)
    return result


class ScanCancelled(Exception):
    """扫描被用户取消"""

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


def save_config(cfg: dict):
    """
    原子写入 scan_config.json。

    与 cache/history/profile 三家一致，收口到 config_io：
      - atomic_write_json：mkstemp 唯一临时名 + 写后回读校验 + os.replace
        （旧的固定临时名 `scan_config.json.tmp` 在并发写下会交错写，
          产出**非法 JSON** 并被 os.replace 发布成正式配置 ⇒
          下次 load_config 命中 JSONDecodeError 返回 {} ⇒ 用户设置整体丢失）
      - config_lock：同目录 <name>.lock 跨进程排他锁
    """
    path = os.path.join(_app_dir(), "scan_config.json")
    try:
        with config_io.config_lock(path):
            config_io.atomic_write_json(path, cfg)
    except Exception as e:
        logger.debug("配置保存失败: %s", e)


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
        self.cfg = load_config()
        return self.cfg

    def save_config(self):
        save_config(self.cfg)

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
        能力查询失败/超时（`fetch_capabilities` 失败，has_adf 保持默认 False）
        时，**保留用户显式选择的来源**并给出提示；绝不静默把「输稿器」
        改成「平板」—— 用户选了输稿器却扫了平板且界面无任何提示，
        正是本轮要消灭的失败模式。

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
                # 能力已确认（查询成功且解析结果里没有 Adf 段）
                # ⇒ 设备真的没有 ADF ⇒ 强制平板。
                if requested == "Feeder":
                    return "Platen", False, WARN_NO_ADF
                return "Platen", False, None
            # 能力未知（查询失败/超时/解析不出 Adf 信息）
            # ⇒ 按用户显式选择走，并把"能力未确认"提示给用户。
            if requested == "Feeder":
                return "Feeder", False, WARN_CAPABILITY_UNKNOWN
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
                    _record_page_meta(job_id, i, data, path)
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
                _record_page_meta(job_id, 1, data, path)

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


def _record_page_meta(job_id: str, page_no: int, data: bytes, cache_path: str) -> None:
    """
    记录单页元数据（原始尺寸 + 缓存路径）。失败不影响扫描结果。

    缓存里保留的永远是**原始扫描件**（不做任何像素改动），A4 归一推迟到
    落盘时做。原始尺寸必须在这里落盘，否则后续的自动纠偏/双面合并/
    断点续扫都没有可回溯的基准（本批次 deskew_angle 先留 0）。
    """
    width = height = 0
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception as e:
        logger.debug("读取页面尺寸失败 [%s]: %s", cache_path, e)
    try:
        store = page_meta.PageMetaStore(cache_manager._cache_root())
        store.set_page(job_id, page_no, page_meta.PageMeta(
            cache_path=cache_path, width=width, height=height))
    except Exception as e:
        logger.debug("写页元数据失败 [%s]: %s", job_id, e)


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
        except ScanCancelled:
            coord.scan_cancelled.emit(job_id)
        except Exception as e:
            logger.error("扫描失败 [%s]: %s", job_id, e, exc_info=True)
            coord.scan_failed.emit(job_id, str(e))
        finally:
            coord._clear_cancel_event(job_id)
