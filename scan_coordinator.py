"""
扫描编排器 — HP Scan Tool v3.3
从 hp_scan_gui 提取的业务逻辑层，不依赖 tkinter。
负责: 配置持久化 / 扫描仪发现与探活 / 扫描执行与缓存 / 保存确认与历史。
"""

import copy
import io
import json
import logging
import os
import threading
from datetime import datetime

import cache_manager
import history_manager
from escl_engine import (
    probe_escl, fetch_capabilities,
    get_scanner_status, execute_scan, execute_multipage_scan,
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
    """应用目录（PyInstaller 兼容）"""
    if getattr(os.sys, 'frozen', False):
        return os.path.dirname(os.sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config() -> dict:
    """读取 scan_config.json"""
    path = os.path.join(_app_dir(), "scan_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict):
    """原子写入 scan_config.json"""
    path = os.path.join(_app_dir(), "scan_config.json")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("配置保存失败: %s", e)


class ScanCoordinator:
    """
    扫描编排器 — 隔离业务逻辑，不依赖 tkinter。
    GUI 层（ScanApp）持有此对象，通过方法调用+回调与之交互。
    """

    def __init__(self):
        self.scanners: list[ScannerInfo] = []
        self._scanners_lock = threading.Lock()
        self._active_scans = 0
        self._scan_lock = threading.Lock()
        self._scan_cancel = threading.Event()
        self.cfg: dict = {}

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
                    new_s = fetch_capabilities(new_s, timeout=3.0)
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
            discovered = discover_all_scanners(timeout=8.0)
            with self._scanners_lock:
                existing_ips = {s.ip for s in self.scanners if s.ip}
            new_scanners = []
            for d in discovered:
                if d.ip not in existing_ips:
                    d.escl_url = probe_escl(d.ip, timeout=4.0) or ""
                    if d.escl_url:
                        d = fetch_capabilities(d, timeout=4.0)
                    new_scanners.append(d)
                    existing_ips.add(d.ip)

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

    def begin_scan(self):
        """递增活跃扫描计数，返回当前计数"""
        with self._scan_lock:
            self._active_scans += 1
            self._scan_cancel.clear()
            return self._active_scans

    def end_scan(self):
        """递减活跃扫描计数，返回剩余计数"""
        with self._scan_lock:
            self._active_scans = max(0, self._active_scans - 1)
            return self._active_scans

    def active_scan_count(self) -> int:
        with self._scan_lock:
            return self._active_scans

    def cancel_scan(self):
        self._scan_cancel.set()

    def is_cancelled(self) -> bool:
        return self._scan_cancel.is_set()

    def execute_scan_to_cache(self, scanner, resolution, color_mode_ui,
                               output_format, source, status_callback=None):
        """
        执行扫描并写入缓存。
        返回 (job_id, ext, page_count, cache_path_or_none)。
        在后台线程调用。
        """
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        cache_manager.register_job(job_id)
        color_mode = COLOR_MODE_MAP.get(color_mode_ui, "RGB24")

        if self.is_cancelled():
            return None

        # 自动检测 ADF
        if scanner.has_adf:
            try:
                st = get_scanner_status(scanner.escl_url, timeout=5.0)
                if st.get("adf_loaded"):
                    source = "Feeder"
                    if status_callback:
                        status_callback("输稿器检测到纸张，自动切换为 ADF 扫描")
                else:
                    source = "Platen"
                    if status_callback:
                        status_callback("输稿器为空，自动切换为平板扫描")
            except Exception as e:
                logger.debug("ADF 状态检测失败，保持手动选择: %s", e)

        use_adf = source == "Feeder"

        if use_adf:
            def _progress(n, _total):
                if status_callback:
                    status_callback(f"正在扫描... 已获取 {n} 页")

            pages = execute_multipage_scan(
                scanner=scanner,
                resolution=resolution,
                color_mode=color_mode,
                output_format=output_format,
                source=source,
                timeout=300.0,
                progress_callback=_progress,
            )
            ext = pages[0][1] if pages else output_format
            for i, (data, ext) in enumerate(pages, 1):
                cache_manager.write_page(job_id, i, data, ext)
                del data
            return (job_id, ext, len(pages), None)
        else:
            data, ext = execute_scan(
                scanner=scanner,
                resolution=resolution,
                color_mode=color_mode,
                output_format=output_format,
                source=source,
                timeout=90.0,
            )
            cache_path = cache_manager.write_page(job_id, 1, data, ext)
            del data
            return (job_id, ext, 1, cache_path)

    def execute_wia_to_cache(self, resolution, color_mode_ui, output_format):
        """WIA 降级扫描并写入缓存。返回 (job_id, ext, cache_path) 或 None。"""
        from wia_engine import try_wia_scan
        color_mode = COLOR_MODE_MAP.get(color_mode_ui, "RGB24")
        result = try_wia_scan(
            resolution=resolution,
            color_mode_name=color_mode,
            output_format=output_format,
        )
        if result:
            data, ext = result
            job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            cache_manager.register_job(job_id)
            cache_path = cache_manager.write_page(job_id, 1, data, ext)
            del data
            return (job_id, ext, cache_path)
        return None

    # ── 缓存清理 ──

    def cleanup_stale_cache(self):
        cache_manager.cleanup_stale()

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
