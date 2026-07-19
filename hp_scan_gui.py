"""
惠普集成扫描工具 v3.3 — CustomTkinter 重写版
功能: 磁盘缓存 / 并发扫描 / 多页ADF / WSD发现 / CLI
"""

import copy
import ctypes
import io
import json
import logging
import os
import shutil
import sys
import threading
import traceback
import tkinter as tk
import tkinter.ttk as ttk
from datetime import datetime
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import customtkinter as ctk
from escl_engine import (
    discover_scanners, probe_escl, fetch_capabilities,
    get_scanner_status, execute_scan, execute_multipage_scan,
    ScannerInfo, FORMAT_MIME, MIME_EXT,
)
from wsd_engine import discover_all_scanners
import cache_manager
import history_manager
from scan_coordinator import (
    ScanCoordinator, record_scan_history, cleanup_cache_job,
    compute_page_groups, delete_cached_page, COLOR_MODE_MAP,
)

# ---------- 资源路径（SOP 7.5.1） ----------
def get_resource_path(relative_path):
    """打包后 __file__ 不可靠，必须使用 sys._MEIPASS 定位资源"""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


# ---------- 嵌入式字体加载（SOP 7.5.5 方案A） ----------
_FONT_FAMILY = "Microsoft YaHei UI"
_FONT_SIZE = 13

def _load_embedded_font():
    """加载打包的思源黑体作为备选字体（主字体为系统 Microsoft YaHei UI）"""
    font_path = get_resource_path(os.path.join("fonts", "NotoSansSC-Regular.ttf"))
    if not os.path.isfile(font_path):
        return  # 字体文件不存在，回退到系统字体

    gdi32 = ctypes.windll.gdi32
    # FR_PRIVATE=0x10, FR_NOT_ENUM=0x20：仅本进程可用，不污染系统字体列表
    ret = gdi32.AddFontResourceExW(font_path, 0x10 | 0x20, 0)
    if ret:
        # 通知系统字体已变更
        HWND_BROADCAST = 0xFFFF
        WM_FONTCHANGE = 0x001D
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW(HWND_BROADCAST, WM_FONTCHANGE, 0, 0, 0x0002, 1000, None)


# ---------- 配置 ----------
def _app_dir():
    # PyInstaller 单文件 EXE：用 EXE 所在目录，避免 tmp 被清理
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(_app_dir(), "scan_config.json")
DEFAULT_OUT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "HP_Scans")

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg):
    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        pass


# ---------- 工具函数 ----------

def _label_for(s: ScannerInfo) -> str:
    if s.custom_name:
        return s.custom_name
    return s.display_name


# ---------- ClearType 渲染修复 ----------
# CustomTkinter 默认使用负数字号（像素尺寸），Windows 下会禁用 ClearType 子像素渲染，
# 退化为灰阶抗锯齿，导致文字发糊。改为正数（点尺寸）以启用 ClearType。
def _patch_ctk_font_scaling():
    """修补 CustomTkinter 字号符号，启用 ClearType 子像素渲染"""
    from customtkinter.windows.widgets.core_widget_classes import CTkBaseClass

    original = CTkBaseClass._apply_font_scaling

    def _patched(self, font):
        result = original(self, font)
        # 将负数（像素尺寸）翻转为正数（点尺寸），启用 ClearType
        if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], int) and result[1] < 0:
            result = (result[0], abs(result[1])) + tuple(result[2:])
        return result

    CTkBaseClass._apply_font_scaling = _patched


# ================================================
#  主窗口
# ================================================

class ScanApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("惠普集成扫描工具")
        self.geometry("840x620")
        self.minsize(720, 500)
        # ★ 设置窗口图标：用 iconphoto + PNG（比 iconbitmap + ICO 更清晰）
        try:
            _icon32 = get_resource_path("icon_32.png")
            _icon64 = get_resource_path("icon_64.png")
            _photo32 = tk.PhotoImage(file=_icon32)
            _photo64 = tk.PhotoImage(file=_icon64)
            self.iconphoto(True, _photo64)  # True = 应用到所有 Toplevel
            self.iconphoto(False, _photo32)
            # 保持引用防止被 GC
            self._icon_photos = [_photo32, _photo64]
        except Exception:
            try:
                _ico = get_resource_path("app.ico")
                self.iconbitmap(default=_ico)
                self.iconbitmap(_ico)
            except Exception:
                pass

        # 先读配置决定主题（在 build 之前）
        _pre_cfg = load_config()
        ctk.set_appearance_mode(_pre_cfg.get("theme", "dark"))
        ctk.set_default_color_theme("blue")

        # ★ 加载遮罩：覆盖整个窗口，盖住 UI 构建过程
        bg = "#2b2b2b" if ctk.get_appearance_mode() == "Dark" else "#f5f5f5"
        self._splash = ctk.CTkFrame(self, fg_color=bg, corner_radius=0)
        self._splash.place(relx=0, rely=0, relwidth=1, relheight=1)
        ctk.CTkLabel(self._splash, text="加载中...",
                     font=ctk.CTkFont(size=16),
                     text_color=("gray30", "gray80")).place(relx=0.5, rely=0.5, anchor="center")
        self.update_idletasks()  # 确保遮罩先渲染出来

        # 加载嵌入式思源黑体（仅本进程生效，不污染系统字体）
        _load_embedded_font()

        # 修补 CustomTkinter 字号符号，启用 ClearType 子像素渲染
        _patch_ctk_font_scaling()

        # 全局字体：使用 ClearType 优化的中文字体，消除模糊
        # CTkFont 默认 Roboto 在本机未安装，回退到系统字体导致渲染模糊
        self.option_add("*Font", (_FONT_FAMILY, _FONT_SIZE))
        ctk.ThemeManager.theme["CTkFont"]["family"] = _FONT_FAMILY

        # 业务逻辑层
        self.coordinator = ScanCoordinator()
        self.coordinator.load_config()
        self.cfg = self.coordinator.cfg

        # 启动时清理过期缓存（防崩溃遗留）
        try:
            self.coordinator.cleanup_stale_cache()
        except Exception:
            pass

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.selected: ScannerInfo | None = None
        self.selected_idx: int = -1

        self.output_dir = self.cfg.get("output_dir", DEFAULT_OUT_DIR)
        self.resolution_val = self.cfg.get("resolution", 300)
        self.color_mode_val = self.cfg.get("color_mode_ui", "彩色")
        self.output_format_val = self.cfg.get("output_format", "jpg")
        self.source_val = self.cfg.get("source", "平板")

        self._build_topbar()
        self._build_main()
        self._build_progress()

        self._restore_presets()

        # ★ 遮罩必须在所有 pack/grid widget 之后 lift 到最上层，
        # 否则 place 的 widget 可能被 pack/grid 的 canvas 覆盖
        self._splash.lift()
        self.update_idletasks()
        self._splash.destroy()
        self._splash = None

        self.after(400, self._auto_discover)

    # ────────── 顶栏 ──────────
    def _build_topbar(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        top.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")

        self.title_lbl = ctk.CTkLabel(left, text="惠普集成扫描工具",
                                      font=ctk.CTkFont(size=22, weight="bold"))
        self.title_lbl.pack(anchor="w")
        ctk.CTkLabel(left, text="eSCL + WIA 双引擎  |  支持 AirScan 网络扫描",
                     font=ctk.CTkFont(size=12),
                     text_color=("gray45", "gray65")).pack(anchor="w", pady=(2, 0))

        self.theme_var = ctk.StringVar(value=ctk.get_appearance_mode().lower())
        ctk.CTkSegmentedButton(
            top,
            values=["light", "dark"],
            variable=self.theme_var,
            command=self._toggle_theme,
            width=100,
        ).grid(row=0, column=2, sticky="e")

    def _toggle_theme(self, value):
        ctk.set_appearance_mode(value)
        self.cfg["theme"] = value
        save_config(self.cfg)

    # ────────── 主区域 ──────────
    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=0, sticky="nsew", padx=14, pady=8)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        # ===== 左侧：扫描仪面板 =====
        left = ctk.CTkFrame(main)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(2, weight=1)

        # 头部
        hdr = ctk.CTkFrame(left, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 2))
        ctk.CTkLabel(hdr, text="扫描仪",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        self.device_count_lbl = ctk.CTkLabel(
            hdr, text="", font=ctk.CTkFont(size=11),
            text_color=("gray45", "gray65"))
        self.device_count_lbl.pack(side="right")

        btn_bar = ctk.CTkFrame(left, fg_color="transparent")
        btn_bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 4))

        self.disc_btn = ctk.CTkButton(btn_bar, text="搜索局域网",
                                       height=34, font=ctk.CTkFont(size=12),
                                       command=self._refresh,
                                       hover_color=("#3a7ebf", "#1f538d"))
        self.disc_btn.pack(side="left", padx=2)
        ctk.CTkButton(btn_bar, text="手动添加", height=34,
                      font=ctk.CTkFont(size=12),
                      fg_color="transparent", border_width=1,
                      text_color=("gray30", "gray80"),
                      border_color=("gray40", "gray60"),
                      hover_color=("gray82", "gray25"),
                      command=self._manual_add).pack(side="left", padx=2)

        # 卡片列表
        self.card_container = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.card_container.grid(row=2, column=0, sticky="nsew", padx=4, pady=(2, 6))
        self.card_container.grid_columnconfigure(0, weight=1)
        self.cards: list["ScannerCard"] = []

        # ===== 右侧：操作面板 =====
        right = ctk.CTkFrame(main)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)

        # 状态条（低调显示，不抢视觉焦点）
        self.status_bar = ctk.CTkLabel(
            right, text="就绪 — 正在搜索打印机...",
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            text_color=("gray35", "gray70"),
            anchor="w", padx=4, pady=2)
        self.status_bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))

        # 参数区标题
        ctk.CTkLabel(right, text="扫描参数",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=1, column=0, sticky="w", padx=12, pady=(8, 4))

        # 参数区（卡片式包裹，紧凑布局）
        params = ctk.CTkFrame(right)
        params.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        params.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self._param(params, "分辨率", ["75", "150", "200", "300", "600", "1200"],
                    str(self.resolution_val), 0, 0,
                    lambda v: setattr(self, 'resolution_val', int(v) if v.isdigit() else 300))
        self._param(params, "颜色", ["彩色", "灰度", "黑白"], self.color_mode_val, 0, 1,
                    lambda v: setattr(self, 'color_mode_val', v))
        self._param(params, "格式", ["jpg", "png", "pdf", "tiff"], self.output_format_val, 0, 2,
                    lambda v: setattr(self, 'output_format_val', v))
        self._param(params, "来源", ["平板"], self.source_val, 0, 3)
        self.src_cb = params.grid_slaves(row=1, column=3)[0]

        # 保存目录（卡片式，与参数区视觉分隔）
        save_row = ctk.CTkFrame(right)
        save_row.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))
        ctk.CTkLabel(save_row, text="保存到",
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(10, 6))
        self.dir_entry = ctk.CTkEntry(save_row)
        self.dir_entry.pack(side="left", padx=(0, 6), fill="x", expand=True)
        self.dir_entry.insert(0, self.output_dir)
        ctk.CTkButton(save_row, text="浏览", width=60, height=34,
                      font=ctk.CTkFont(size=12),
                      fg_color="transparent", border_width=1,
                      text_color=("gray30", "gray80"),
                      border_color=("gray40", "gray60"),
                      hover_color=("gray82", "gray25"),
                      command=self._browse).pack(side="right", padx=(0, 8))

        # 操作按钮行（四个按钮等高等宽，扫描按钮用实心蓝色区分主操作）
        action = ctk.CTkFrame(right, fg_color="transparent")
        action.grid(row=4, column=0, sticky="ew", padx=12, pady=(4, 2))
        action.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.caps_btn = ctk.CTkButton(action, text="查询能力",
                                       height=36,
                                       font=ctk.CTkFont(size=12),
                                       border_width=1,
                                       text_color=("gray30", "gray80"),
                                       border_color=("gray40", "gray60"),
                                       fg_color="transparent",
                                       hover_color=("gray82", "gray25"),
                                       command=self._query_caps)
        self.caps_btn.grid(row=0, column=0, sticky="ew", padx=2)

        self.stat_btn = ctk.CTkButton(action, text="预览状态",
                                       height=36,
                                       font=ctk.CTkFont(size=12),
                                       border_width=1,
                                       text_color=("gray30", "gray80"),
                                       border_color=("gray40", "gray60"),
                                       fg_color="transparent",
                                       hover_color=("gray82", "gray25"),
                                       command=self._check_status)
        self.stat_btn.grid(row=0, column=1, sticky="ew", padx=2)

        ctk.CTkButton(action, text="扫描历史",
                      height=36,
                      font=ctk.CTkFont(size=12),
                      border_width=1,
                      text_color=("gray30", "gray80"),
                      border_color=("gray40", "gray60"),
                      fg_color="transparent",
                      hover_color=("gray82", "gray25"),
                      command=self._show_history).grid(row=0, column=2, sticky="ew", padx=2)

        self.scan_btn = ctk.CTkButton(action, text="扫描",
                                       height=36,
                                       font=ctk.CTkFont(size=12, weight="bold"),
                                       command=self._start_scan)
        self.scan_btn.grid(row=0, column=3, sticky="ew", padx=2)

        # 扫描进度条（初始隐藏，扫描时显示）
        self.scan_progress = ctk.CTkProgressBar(right, height=10)
        self.scan_progress.grid(row=5, column=0, sticky="ew", padx=12, pady=(2, 8))
        self.scan_progress.grid_remove()
        self.scan_progress.set(0)

    def _param(self, parent, label, values, default, row, col, command=None):
        ctk.CTkLabel(parent, text=label,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=col, padx=8, pady=(8, 0), sticky="w")
        cb = ctk.CTkComboBox(parent, values=values, state="readonly", width=100, command=command)
        cb.set(default)
        cb.grid(row=1, column=col, padx=8, pady=(2, 8))

    # ────────── 窗口关闭清理 ──────────
    def destroy(self):
        self.coordinator.cancel_scan()
        if hasattr(self, 'progress'):
            try:
                self.progress.stop()
            except Exception:
                pass
        try:
            super().destroy()
        except Exception:
            pass
        # 强制退出，防止后台线程（zeroconf mDNS、requests keep-alive）阻止进程终止
        os._exit(0)

    # ────────── 进度条 ──────────
    def _build_progress(self):
        self.progress = ctk.CTkProgressBar(self)
        self.progress.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.progress.grid_remove()
        self.progress.configure(mode="indeterminate")

    def _set_taskbar_icon(self):
        """用 Windows API 设置任务栏图标（精确尺寸，避免缩放模糊）"""
        try:
            # 获取窗口句柄（需要 GetParent 获取真正的顶层窗口）
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if not hwnd:
                hwnd = self.winfo_id()
            if not hwnd:
                return
            
            ico_path = getattr(self, '_ico_path', None)
            if not ico_path:
                return
            
            # LR_LOADFROMFILE=0x0010, IMAGE_ICON=1
            # width=0, height=0 让 Windows 自动选择 ICO 中最合适的尺寸
            hicon = ctypes.windll.user32.LoadImageW(
                0, ico_path, 1, 0, 0, 0x0010)
            if hicon:
                # WM_SETICON=0x0080, ICON_SMALL=0, ICON_BIG=1
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, hicon)
                ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, hicon)
        except Exception:
            pass

    # ────────── 扫描仪卡片 ──────────
    def _rebuild_cards(self):
        for c in self.cards:
            c.destroy()
        self.cards.clear()

        for i, s in enumerate(self.coordinator.scanners):
            card = ScannerCard(self.card_container, s, i, self)
            card.grid(row=i, column=0, sticky="ew", pady=3)
            self.cards.append(card)

        # 恢复选中状态高亮
        if 0 <= self.selected_idx < len(self.cards):
            self.cards[self.selected_idx].set_selected(True)

        # 更新设备计数
        n = len(self.coordinator.scanners)
        self.device_count_lbl.configure(text=f"{n} 台" if n else "")

    # ────────── 发现 ──────────
    def _restore_presets(self):
        saved_ips = self.cfg.get("saved_ips", [])
        nicknames = self.cfg.get("nicknames", {})
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
            si.escl_url = ""  # 启动时不同步探活，后台再补
            si.custom_name = nicknames.get(ip, "")
            self.coordinator.scanners.append(si)

        self._rebuild_cards()
        if self.coordinator.scanners:
            self.status_bar.configure(text=f"已加载 {len(self.coordinator.scanners)} 台历史打印机")
            # 后台异步探活，不阻塞 UI
            threading.Thread(target=self._probe_saved, daemon=True).start()

    def _probe_saved(self):
        """后台逐台探活已加载的打印机（只读快照，结果回主线程原子替换）"""
        snapshot = list(self.coordinator.scanners)  # 主线程已完成的快照，后台只读
        results = {}
        for s in snapshot:
            url = probe_escl(s.ip, port=s.port, timeout=3.0)
            if url:
                new_s = copy.copy(s)
                new_s.escl_url = url
                new_s = fetch_capabilities(new_s, timeout=3.0)
                results[s.ip] = new_s
        self.after(0, lambda: self._apply_probe_results(results))
        self.after(0, self._rebuild_cards)

    def _apply_probe_results(self, results: dict):
        """主线程：原子应用探活结果"""
        for i, s in enumerate(self.coordinator.scanners):
            if s.ip in results:
                self.coordinator.scanners[i] = results[s.ip]
                if self.selected and self.selected.ip == s.ip:
                    self.selected = results[s.ip]
        # 探活完成后，如果还没有选中扫描仪，尝试恢复上次选中的
        if self.selected is None:
            saved_ip = self.cfg.get("selected_scanner_ip", "")
            if saved_ip:
                for i, s in enumerate(self.coordinator.scanners):
                    if s.ip == saved_ip:
                        self.select_scanner(i)
                        break

    def _auto_discover(self):
        # 已有历史打印机则跳过自动搜索，直接加载
        if self.coordinator.scanners:
            n = len(self.coordinator.scanners)
            self.status_bar.configure(
                text=f"已加载 {n} 台历史打印机 — 单击选中后按「扫描」")
            return
        self._set_loading(True)
        threading.Thread(target=self._do_discover, daemon=True).start()

    def _refresh(self):
        self.status_bar.configure(text="正在搜索局域网...")
        self._set_loading(True)
        threading.Thread(target=self._do_discover, daemon=True).start()

    def _do_discover(self):
        discovered = discover_all_scanners(timeout=4.0)
        with self.coordinator._scanners_lock:
            existing_ips = {s.ip for s in self.coordinator.scanners if s.ip}  # 快照读取
        new_scanners = []
        new_count = 0
        for d in discovered:
            if d.ip not in existing_ips:
                d.escl_url = probe_escl(d.ip, timeout=3.0) or ""
                if d.escl_url:
                    d = fetch_capabilities(d, timeout=3.0)
                new_scanners.append(d)
                existing_ips.add(d.ip)
                new_count += 1

        # 构建持久化数据（在后台线程完成，不回读 self.coordinator.scanners）
        with self.coordinator._scanners_lock:
            all_for_save = list(self.coordinator.scanners) + new_scanners
        nicknames = self.cfg.get("nicknames", {})
        ips = []
        for s in all_for_save:
            if s.ip:
                ips.append({"ip": s.ip, "model": s.model})
                if s.custom_name:
                    nicknames[s.ip] = s.custom_name
        self.cfg["saved_ips"] = ips
        self.cfg["nicknames"] = nicknames
        save_config(self.cfg)

        self.after(0, lambda: self._on_discover_done(new_count, new_scanners))

    def _on_discover_done(self, new_count, new_scanners):
        self.coordinator.scanners.extend(new_scanners)  # 主线程写入，安全
        self._set_loading(False)
        self._rebuild_cards()
        n = len(self.coordinator.scanners)
        self.status_bar.configure(
            text=f"发现 {n} 台打印机 (+{new_count} 新增) — 单击选中后按「扫描」" if n
            else "未发现打印机，请确认电源和网络，或手动添加 IP")
        # 恢复上次选中的扫描仪（按 IP 匹配）
        saved_ip = self.cfg.get("selected_scanner_ip", "")
        restored = False
        if saved_ip:
            for i, s in enumerate(self.coordinator.scanners):
                if s.ip == saved_ip:
                    self.select_scanner(i)
                    restored = True
                    break
        if not restored:
            self.selected = None
            self.selected_idx = -1

    def _set_loading(self, active):
        if active:
            self.progress.grid()
            self.progress.start()
            self.disc_btn.configure(state="disabled", text="搜索中...")
        else:
            self.progress.stop()
            self.progress.grid_remove()
            self.disc_btn.configure(state="normal", text="搜索局域网")

    def _update_source_options(self, has_adf: bool):
        """根据扫描仪能力更新来源下拉框"""
        sources = ["平板"]
        if has_adf:
            sources.append("输稿器(ADF)")
        self.src_cb.configure(values=sources)
        if self.source_val in sources:
            self.src_cb.set(self.source_val)
        else:
            self.src_cb.set(sources[0])
            self.source_val = sources[0]

    # ────────── 选择 ──────────
    def select_scanner(self, idx: int):
        if 0 <= idx < len(self.coordinator.scanners):
            self.selected = self.coordinator.scanners[idx]
            self.selected_idx = idx
            self._update_source_options(self.selected.has_adf)

            label = _label_for(self.selected)
            self.status_bar.configure(text=f"已选择: {label}")

            for i, card in enumerate(self.cards):
                card.set_selected(i == idx)

            # 持久化选中的扫描仪 IP
            if self.selected.ip:
                self.cfg["selected_scanner_ip"] = self.selected.ip
                save_config(self.cfg)

    # ────────── 自定义名称 ──────────
    def set_custom_name(self, idx: int, name: str):
        if 0 <= idx < len(self.coordinator.scanners):
            self.coordinator.scanners[idx].custom_name = name.strip() or ""
            nicknames = self.cfg.get("nicknames", {})
            ip = self.coordinator.scanners[idx].ip
            if self.coordinator.scanners[idx].custom_name:
                nicknames[ip] = self.coordinator.scanners[idx].custom_name
            else:
                nicknames.pop(ip, None)
            self.cfg["nicknames"] = nicknames
            save_config(self.cfg)

            self._rebuild_cards()
            if self.selected_idx == idx:
                label = _label_for(self.coordinator.scanners[idx])
                self.status_bar.configure(text=f"已选择: {label}")

    # ────────── 调整打印机优先级 ──────────
    def move_scanner(self, idx: int, direction: int):
        """移动打印机在列表中的位置（direction: -1=上移, +1=下移）"""
        new_idx = idx + direction
        if not (0 <= new_idx < len(self.coordinator.scanners)):
            return
        
        # 交换位置
        scanners = self.coordinator.scanners
        scanners[idx], scanners[new_idx] = scanners[new_idx], scanners[idx]
        
        # 更新选中索引
        if self.selected_idx == idx:
            self.selected_idx = new_idx
        elif self.selected_idx == new_idx:
            self.selected_idx = idx
        
        # 保存排序到配置
        ips = []
        for s in scanners:
            if s.ip:
                ips.append({"ip": s.ip, "model": s.model})
        self.cfg["saved_ips"] = ips
        save_config(self.cfg)
        
        self._rebuild_cards()

    # ────────── 手动添加 ──────────
    def _manual_add(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("手动添加打印机")
        dlg.geometry("420x300")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="输入打印机 IP 地址",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(15, 5))
        ctk.CTkLabel(dlg, text="程序会自动探测 eSCL 服务并拉取能力",
                     font=ctk.CTkFont(size=11),
                     text_color=("gray45", "gray65")).pack()

        ip_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        ip_frame.pack(pady=10)

        ip_var = ctk.StringVar()
        ctk.CTkEntry(ip_frame, width=200, placeholder_text="例如: 192.168.1.100",
                     textvariable=ip_var).pack(side="left", padx=(0, 6))

        ctk.CTkLabel(ip_frame, text="端口", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 4))
        port_var = ctk.StringVar(value="80")
        ctk.CTkEntry(ip_frame, width=60, textvariable=port_var).pack(side="left")

        result_lbl = ctk.CTkLabel(dlg, text="")
        result_lbl.pack()
        add_btn = ctk.CTkButton(dlg, text="探测并添加", width=140)
        add_btn.pack(pady=8)

        def probe():
            ip = ip_var.get().strip()
            if not ip:
                return
            try:
                port = int(port_var.get().strip() or "80")
            except ValueError:
                port = 80
            result_lbl.configure(text="探测中...", text_color=("gray45", "gray65"))
            add_btn.configure(state="disabled")

            def do_probe():
                url = probe_escl(ip, port=port, timeout=4.0)
                # 回到主线程更新 UI（先检查对话框是否还活着）
                dlg.after(0, lambda: _on_probe_done(url, ip, port))

            def _on_probe_done(url, ip, port):
                if not dlg.winfo_exists():
                    return
                add_btn.configure(state="normal")
                if url:
                    s = ScannerInfo(name="手动添加的打印机", ip=ip, port=port, escl_url=url)
                    s = fetch_capabilities(s, timeout=4.0)
                    self.coordinator.scanners.append(s)
                    self._rebuild_cards()
                    self.status_bar.configure(text=f"已添加 {_label_for(s)}")
                    result_lbl.configure(text="成功 — eSCL 可用", text_color="#2a9d8f")
                    dlg.after(800, dlg.destroy)
                else:
                    result_lbl.configure(text="该 IP 不支持 eSCL，请确认打印机已启用 AirScan",
                                         text_color="#e07b5a")

            threading.Thread(target=do_probe, daemon=True).start()

        add_btn.configure(command=probe)

    # ────────── 异步工具 ──────────
    def _run_async(self, task, callback=None):
        """后台线程执行 task()，完成后在主线程调用 callback(result)。
        异常时 callback 接收 Exception 对象；无 callback 则仅 logging。
        """
        def _worker():
            try:
                result = task()
            except Exception as e:
                logging.debug("异步任务失败: %s", e)
                result = e
            if callback:
                self.after(0, lambda: callback(result))
        threading.Thread(target=_worker, daemon=True).start()

    # ────────── 能力查询 ──────────
    def _query_caps(self):
        if not self.selected:
            messagebox.showwarning("提示", "请先选择一台打印机")
            return
        if not self.selected.escl_url:
            messagebox.showwarning("提示", "该打印机未配置 eSCL URL")
            return

        self._set_loading(True)
        scanner_ref = self.selected
        self._run_async(
            lambda: fetch_capabilities(scanner_ref, timeout=5.0),
            self._apply_caps,
        )

    def _apply_caps(self, s):
        """主线程：原子应用能力查询结果"""
        for i, sc in enumerate(self.coordinator.scanners):
            if sc.ip == s.ip:
                self.coordinator.scanners[i] = s
                break
        self._caps_done(s)

    def _caps_done(self, s):
        self._set_loading(False)
        self._rebuild_cards()
        self._update_source_options(s.has_adf)

        info = (
            f"型号: {s.model or '未知'}\n"
            f"序列号: {s.serial or '未知'}\n"
            f"最大分辨率: {max(s.resolutions) if s.resolutions else '?'} DPI\n"
            f"ADF: {'有' if s.has_adf else '无'}  双面: {'支持' if s.has_duplex else '不支持'}\n"
            f"平板: {'有' if s.has_platen else '无'}\n"
            f"颜色模式: {', '.join(s.color_modes) if s.color_modes else '未知'}\n"
            f"输出格式: {', '.join(s.formats) if s.formats else '未知'}"
        )
        messagebox.showinfo(f"{_label_for(s)} 能力", info)

    # ────────── 状态 ──────────
    def _check_status(self):
        if not self.selected or not self.selected.escl_url:
            messagebox.showwarning("提示", "请先选择一台支持 eSCL 的打印机")
            return

        url = self.selected.escl_url  # 捕获到局部变量，防止切换打印机后读到新值
        self._run_async(
            lambda: get_scanner_status(url, timeout=4.0),
            lambda st: messagebox.showinfo(
                "打印机状态",
                f"状态: {st.get('state', '未知')}\nADF 有纸: {'是' if st.get('adf_loaded') else '否'}"
            ),
        )

    # ────────── 浏览 ──────────
    def _browse(self):
        p = filedialog.askdirectory(title="选择保存目录")
        if p:
            self.output_dir = p
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, p)
            self.cfg["output_dir"] = p
            save_config(self.cfg)

    # ────────── 扫描 ──────────
    def _start_scan(self):
        if not self.selected:
            messagebox.showwarning("提示", "请先选择一台打印机")
            return

        self.coordinator._scan_cancel.clear()  # 重置取消标志
        if not self.selected.escl_url:
            messagebox.showinfo("提示",
                "该打印机未探测到 eSCL，将尝试 WIA 扫描。\n如失败请确认打印机已连接并安装驱动。")
            self._wia_scan()
            return

        out_dir = self.dir_entry.get() or self.output_dir
        # 验证路径有效性
        try:
            out_dir = os.path.abspath(out_dir)
        except (ValueError, OSError):
            messagebox.showerror("错误", "保存路径无效，请重新选择")
            return
        if not os.path.exists(out_dir):
            try:
                os.makedirs(out_dir)
            except Exception as e:
                messagebox.showerror("错误", f"无法创建目录: {e}")
                return
        if not os.path.isdir(out_dir):
            messagebox.showerror("错误", "保存路径不是目录，请重新选择")
            return

        # 捕获当前选中的扫描仪和参数（避免并发时 self.selected 被切换）
        scanner = self.selected
        source_val = "Platen" if "平板" in self.src_cb.get() else "Feeder"

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_tag = scanner.model.replace(" ", "_") if scanner.model else "scan"
        fname = f"HP_{model_tag}_{ts}.{self.output_format_val}"
        fpath = os.path.join(out_dir, fname)

        self.cfg.update({
            "resolution": self.resolution_val,
            "color_mode_ui": self.color_mode_val,
            "output_format": self.output_format_val,
            "output_dir": out_dir,
            "source": self.src_cb.get(),
        })
        save_config(self.cfg)

        # 增加活跃扫描计数
        count = self.coordinator.begin_scan()
        self.scan_btn.configure(text=f"扫描中 ({count})...", state="disabled")
        self.status_bar.configure(text=f"活跃扫描任务: {count}")
        # 显示进度条
        self.scan_progress.grid()
        self.scan_progress.set(0)

        threading.Thread(target=self._do_capture, args=(
            scanner, fpath, source_val,
        ), daemon=True).start()

    def _do_capture(self, scanner, output_path, source_val):
        """扫描到内存，然后弹出预览对话框"""
        try:
            if self.coordinator.is_cancelled():
                self.after(0, lambda: self._reset_scan_ui())
                return

            self.after(0, lambda: self.status_bar.configure(text="正在创建扫描任务..."))

            # 自动检测 ADF
            if scanner.has_adf:
                try:
                    st = get_scanner_status(scanner.escl_url, timeout=5.0)
                    if st.get("adf_loaded"):
                        source_val = "Feeder"
                        self.after(0, lambda: self.status_bar.configure(
                            text="输稿器检测到纸张，自动切换为 ADF 扫描"))
                    else:
                        source_val = "Platen"
                        self.after(0, lambda: self.status_bar.configure(
                            text="输稿器为空，自动切换为平板扫描"))
                except Exception as e:
                    logging.debug("ADF 状态检测失败，保持手动选择: %s", e)

            self.after(0, lambda: self.status_bar.configure(text="正在扫描..."))

            job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            cache_manager.register_job(job_id)
            use_adf = source_val == "Feeder"

            if use_adf:
                # 多页 ADF 扫描
                def _progress(n, _total):
                    self.after(0, lambda n=n: (
                        self.status_bar.configure(text=f"正在扫描... 已获取 {n} 页"),
                        self.scan_progress.set(min(0.95, n * 0.15))
                    ))

                pages = execute_multipage_scan(
                    scanner=scanner,
                    resolution=self.resolution_val,
                    color_mode=COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
                    output_format=self.output_format_val,
                    source=source_val,
                    timeout=300.0,
                    progress_callback=_progress,
                )
                self.after(0, lambda: self.scan_progress.set(0.95))
                # 写入缓存
                ext = pages[0][1] if pages else self.output_format_val
                for i, (data, ext) in enumerate(pages, 1):
                    cache_manager.write_page(job_id, i, data, ext)
                    del data

                self.after(0, lambda: self.scan_progress.set(1.0))
                if len(pages) > 1:
                    self.after(0, lambda s=scanner: self._show_multi_preview(job_id, ext, output_path, len(pages), scanner=s))
                else:
                    cache_path = cache_manager._job_dir(job_id) + f"/page_001.{ext}"
                    self.after(0, lambda s=scanner: self._show_preview(cache_path, ext, output_path, job_id, scanner=s))
            else:
                # 单页平板扫描
                self.after(0, lambda: self.scan_progress.set(0.3))
                data, ext = execute_scan(
                    scanner=scanner,
                    resolution=self.resolution_val,
                    color_mode=COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
                    output_format=self.output_format_val,
                    source=source_val,
                    timeout=90.0,
                )
                self.after(0, lambda: self.scan_progress.set(0.8))
                cache_path = cache_manager.write_page(job_id, 1, data, ext)
                del data
                self.after(0, lambda: self.scan_progress.set(1.0))
                self.after(0, lambda s=scanner: self._show_preview(cache_path, ext, output_path, job_id, scanner=s))
        except Exception as e:
            tb = traceback.format_exc()
            self.after(0, lambda: self._scan_error(f"{type(e).__name__}: {e}\n\n{tb}"))

    def _wia_scan(self):
        out_dir = self.dir_entry.get() or self.output_dir
        if not os.path.exists(out_dir):
            try:
                os.makedirs(out_dir)
            except Exception:
                pass

        scanner = self.selected
        count = self.coordinator.begin_scan()
        self.scan_btn.configure(text=f"扫描中 ({count})...", state="disabled")
        self.status_bar.configure(text=f"活跃扫描任务: {count}")
        # 显示进度条
        self.scan_progress.grid()
        self.scan_progress.set(0)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_tag = scanner.model.replace(" ", "_") if scanner.model else "scan"
        fpath = os.path.join(out_dir, f"WIA_{model_tag}_{ts}.{self.output_format_val}")

        def do():
            from wia_engine import try_wia_scan
            self.after(0, lambda: self.scan_progress.set(0.3))
            result = try_wia_scan(
                resolution=self.resolution_val,
                color_mode_name=COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
                output_format=self.output_format_val,
            )
            if result:
                data, ext = result
                self.after(0, lambda: self.scan_progress.set(0.8))
                job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                cache_manager.register_job(job_id)
                cache_path = cache_manager.write_page(job_id, 1, data, ext)
                del data
                self.after(0, lambda: self.scan_progress.set(1.0))
                self.after(0, lambda s=scanner: self._show_preview(cache_path, ext, fpath, job_id, scanner=s))
            else:
                self.after(0, lambda: self._scan_error("WIA 扫描失败，未找到可用扫描仪"))

        threading.Thread(target=do, daemon=True).start()

    def _reset_scan_ui(self):
        """完成一个扫描任务后调用，递减计数"""
        count = self.coordinator.end_scan()
        if count == 0:
            self.scan_btn.configure(text="扫描", state="normal")
            self._set_loading(False)
            # 隐藏进度条
            self.scan_progress.grid_remove()
            self.scan_progress.set(0)
        else:
            self.scan_btn.configure(text=f"扫描中 ({count})...")

    def _scan_error(self, msg):
        self._reset_scan_ui()
        count = self.coordinator.active_scan_count()
        self.status_bar.configure(text=f"扫描失败 (活跃任务: {count})")
        messagebox.showerror("扫描失败", f"{msg}\n\n如果 eSCL 不通，请确认:\n"
                            "1. 打印机已启用 eSCL/AirScan\n"
                            "2. 浏览器访问 http://打印机IP/eSCL/ScannerStatus 确认可达\n"
                            "3. 防火墙未拦截")

    def _show_preview(self, cache_path, ext, output_path, job_id, scanner=None):
        self._reset_scan_ui()
        count = self.coordinator.active_scan_count()
        self.status_bar.configure(text=f"扫描完成 — 点击保存 (活跃: {count})")
        dev_name = (scanner.model or "") if scanner else ""
        dev_ip = scanner.ip if scanner else ""
        PreviewDialog(self, cache_path, ext, output_path, job_id,
                      device_name=dev_name, device_ip=dev_ip)

    def _show_multi_preview(self, job_id, ext, output_path, page_count, scanner=None):
        self._reset_scan_ui()
        count = self.coordinator.active_scan_count()
        self.status_bar.configure(text=f"扫描完成 — {page_count} 页 (活跃: {count})")
        dev_name = (scanner.model or "") if scanner else ""
        dev_ip = scanner.ip if scanner else ""
        MultiPagePreviewDialog(self, job_id, ext, output_path, page_count,
                               device_name=dev_name, device_ip=dev_ip)

    def _show_history(self):
        HistoryDialog(self)


# ================================================
#  扫描预览对话框
# ================================================

class PreviewDialog(ctk.CTkToplevel):
    """扫描后预览：确认后保存"""

    def __init__(self, parent, cache_path: str, ext: str, output_path: str, job_id: str,
                 device_name: str = "", device_ip: str = ""):
        super().__init__(parent)
        self.title("扫描预览")
        self.geometry("540x520")
        self.minsize(460, 420)
        self.resizable(True, True)
        self.transient(parent)
        try:
            _p = tk.PhotoImage(file=get_resource_path("icon_64.png"))
            self.iconphoto(True, _p)
            self._icon_photo = _p
        except Exception:
            try:
                self.iconbitmap(get_resource_path("app.ico"))
            except Exception:
                pass

        self.cache_path = cache_path
        self.ext = ext
        self.output_path = output_path
        self.job_id = job_id
        self.device_name = device_name
        self.device_ip = device_ip
        self._preview_photo = None

        self._build()
        self.grab_set()

        # 居中
        x = parent.winfo_x() + (parent.winfo_width() - 540) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 520) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.after_idle(self._show_image)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _build(self):
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(side="bottom", fill="x", padx=14, pady=(4, 14))

        ctk.CTkButton(bf, text="重新扫描", width=100, height=34,
                      fg_color="transparent", border_width=1,
                      text_color=("gray30", "gray80"),
                      border_color=("gray40", "gray60"),
                      command=self._cancel).pack(side="left")
        ctk.CTkButton(bf, text="保存", width=140, height=38,
                      font=ctk.CTkFont(size=14, weight="bold"),
                      command=self._confirm).pack(side="right")

        pf = ctk.CTkFrame(self)
        pf.pack(fill="both", expand=True, padx=14, pady=(14, 6))
        pf.grid_columnconfigure(0, weight=1)
        pf.grid_rowconfigure(0, weight=1)
        self.img_lbl = ctk.CTkLabel(pf, text="正在加载预览...",
                                    fg_color=("gray85", "gray25"),
                                    corner_radius=6)
        self.img_lbl.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        self.bind("<Configure>", self._on_resize)
        self._last_size = (0, 0)

    def _show_image(self):
        try:
            img = Image.open(self.cache_path)
            w = max(200, self.winfo_width() - 60)
            h = max(150, self.winfo_height() - 120)
            img.thumbnail((w, h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._preview_photo = photo
            self.img_lbl.configure(image=photo, text="")
        except Exception as e:
            self.img_lbl.configure(image=None, text=f"预览失败: {e}")

    def _on_resize(self, event=None):
        if event is None:
            return
        w, h = event.width, event.height
        if abs(w - self._last_size[0]) < 30 and abs(h - self._last_size[1]) < 30:
            return
        self._last_size = (w, h)
        self._show_image()

    def _confirm(self):
        default_dir = os.path.dirname(self.output_path)
        default_name = os.path.basename(self.output_path)
        out_ext = default_name.rsplit(".", 1)[-1].lower()

        _filters = [
            ("JPEG 图片", "*.jpg *.jpeg"),
            ("PNG 图片", "*.png"),
            ("TIFF 图片", "*.tiff *.tif"),
            ("PDF 文档", "*.pdf"),
        ]
        ext_to_filter = {"jpg": 0, "jpeg": 0, "png": 1, "tiff": 2, "tif": 2, "pdf": 3}
        idx = ext_to_filter.get(out_ext, 0)
        _filters.insert(0, _filters.pop(idx))

        save_path = filedialog.asksaveasfilename(
            parent=self, title="保存扫描文件",
            initialdir=default_dir, initialfile=default_name,
            defaultextension=f".{out_ext}", filetypes=_filters)
        if not save_path:
            return

        out_ext = save_path.rsplit(".", 1)[-1].lower()

        # 直接从缓存读取原始数据保存（不做曝光处理）
        if out_ext == "pdf":
            img = Image.open(self.cache_path)
            pdf_path = save_path if save_path.lower().endswith(".pdf") \
                       else save_path.rsplit(".", 1)[0] + ".pdf"
            img.convert("RGB").save(pdf_path, "PDF", resolution=150.0)
            saved = pdf_path
        else:
            # 直接复制缓存文件（避免重新编码损失质量）
            final = save_path
            if not final.lower().endswith(f".{out_ext}"):
                final = f"{final.rsplit('.', 1)[0]}.{out_ext}"
            cache_src = self.cache_path
            if out_ext == "jpg" or out_ext == "jpeg":
                shutil.copy2(cache_src, final)
            else:
                img = Image.open(cache_src)
                if out_ext == "png":
                    img.save(final, "PNG")
                elif out_ext == "tiff" or out_ext == "tif":
                    img.save(final, "TIFF")
                else:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(final, "JPEG", quality=95)
            saved = final

        cache_manager.remove_job(self.job_id)

        history_manager.append({
            "device_name": self.device_name,
            "device_ip": self.device_ip,
            "page_count": 1,
            "format": out_ext,
            "file_path": saved,
        })

        self.destroy()
        os.startfile(os.path.dirname(saved))

    def _cancel(self):
        cache_manager.remove_job(self.job_id)
        self.destroy()


# ================================================
#  多页批量预览对话框（ADF 扫描后）
# ================================================

class MultiPagePreviewDialog(ctk.CTkToplevel):
    """多页 ADF 扫描后的批量预览：缩略图导航、删除、分组管理"""

    TW, TH = 80, 100

    def __init__(self, parent, job_id: str, ext: str, output_path: str, page_count: int,
                 device_name: str = "", device_ip: str = ""):
        super().__init__(parent)
        self.title(f"扫描预览 — {page_count} 页")
        self.geometry("600x700")
        self.minsize(500, 600)
        self.resizable(True, True)
        self.transient(parent)
        try:
            _p = tk.PhotoImage(file=get_resource_path("icon_64.png"))
            self.iconphoto(True, _p)
            self._icon_photo = _p
        except Exception:
            try:
                self.iconbitmap(get_resource_path("app.ico"))
            except Exception:
                pass

        self.job_id = job_id
        self.ext = ext
        self.output_path = output_path
        self.device_name = device_name
        self.device_ip = device_ip

        self.pages = list(range(1, page_count + 1))
        self.selected_idx = 0
        self.splits = set()
        self.group_names = {}
        self._preview_photo = None
        self._thumb_photos = {}

        self._build()
        self.grab_set()

        x = parent.winfo_x() + (parent.winfo_width() - 600) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 700) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.after_idle(self._refresh_thumbs)
        self.after(100, self._update_preview)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _cache_path(self, page_num):
        return os.path.join(cache_manager._job_dir(self.job_id),
                            f"page_{page_num:03d}.{self.ext}")

    def _build(self):
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(side="bottom", fill="x", padx=14, pady=(4, 14))

        ctk.CTkButton(bf, text="重新扫描", width=90, height=32,
                      fg_color="transparent", border_width=1,
                      text_color=("gray30", "gray80"),
                      border_color=("gray40", "gray60"),
                      command=self._cancel).pack(side="left")
        self.save_btn = ctk.CTkButton(bf, text="保存全部", width=140, height=36,
                                       font=ctk.CTkFont(size=14, weight="bold"),
                                       command=self._confirm)
        self.save_btn.pack(side="right")

        pf = ctk.CTkFrame(self)
        pf.pack(fill="both", expand=True, padx=14, pady=(14, 4))
        pf.grid_columnconfigure(0, weight=1)
        pf.grid_rowconfigure(0, weight=1)
        self.img_lbl = ctk.CTkLabel(pf, text="加载中...",
                                    fg_color=("gray85", "gray25"),
                                    corner_radius=6)
        self.img_lbl.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        self.bind("<Configure>", self._on_resize)
        self._last_size = (0, 0)

        self.page_info_lbl = ctk.CTkLabel(pf, text="",
                                           font=ctk.CTkFont(size=11),
                                           text_color=("gray45", "gray65"))
        self.page_info_lbl.grid(row=1, column=0, padx=6, pady=(0, 4))

        tf = ctk.CTkFrame(self)
        tf.pack(fill="x", padx=14, pady=4)
        self.thumb_container = ctk.CTkScrollableFrame(tf, fg_color="transparent",
                                                       orientation="horizontal", height=self.TH + 20)
        self.thumb_container.pack(fill="x", padx=4, pady=4)

        gf = ctk.CTkFrame(self, fg_color="transparent")
        gf.pack(fill="x", padx=14, pady=(0, 2))
        ctk.CTkButton(gf, text="在此页后插入分割", width=110, height=26,
                      font=ctk.CTkFont(size=10),
                      fg_color=("gray75", "gray35"),
                      hover_color=("gray65", "gray45"),
                      command=self._insert_split).pack(side="left", padx=4)
        ctk.CTkButton(gf, text="移除分割", width=80, height=26,
                      font=ctk.CTkFont(size=10),
                      fg_color=("gray75", "gray35"),
                      hover_color=("gray65", "gray45"),
                      command=self._remove_split).pack(side="left", padx=4)
        ctk.CTkButton(gf, text="重命名分组", width=90, height=26,
                      font=ctk.CTkFont(size=10),
                      fg_color=("gray75", "gray35"),
                      hover_color=("gray65", "gray45"),
                      command=self._rename_group).pack(side="left", padx=4)
        self.group_info_lbl = ctk.CTkLabel(gf, text="1 个分组",
                                            font=ctk.CTkFont(size=10),
                                            text_color=("gray45", "gray65"))
        self.group_info_lbl.pack(side="right", padx=8)

    def _get_groups(self):
        split_positions = sorted(self.splits)
        groups = []
        start = 0
        for sp in split_positions:
            if sp + 1 < len(self.pages):
                groups.append(list(range(start, sp + 1)))
                start = sp + 1
        groups.append(list(range(start, len(self.pages))))
        return [g for g in groups if g]

    def _refresh_thumbs(self):
        for w in self.thumb_container.winfo_children():
            w.destroy()
        self._thumb_photos.clear()

        groups = self._get_groups()
        group_of_page = {}
        for gi, g in enumerate(groups):
            for pi in g:
                group_of_page[pi] = gi

        n_groups = len(groups)
        self.group_info_lbl.configure(text=f"{n_groups} 个分组")

        for idx, page_num in enumerate(self.pages):
            if idx > 0 and (idx - 1) in self.splits:
                sep = ctk.CTkFrame(self.thumb_container, width=3,
                                    height=self.TH + 16,
                                    fg_color="#3B8ED0")
                sep.pack(side="left", padx=4, pady=2)
                sep.pack_propagate(False)

            frame = ctk.CTkFrame(self.thumb_container, fg_color="transparent")
            frame.pack(side="left", padx=2)

            try:
                img = Image.open(self._cache_path(page_num))
                img.thumbnail((self.TW, self.TH), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._thumb_photos[idx] = photo
            except Exception:
                photo = None

            gi = group_of_page.get(idx, 0)
            group_label = self.group_names.get(gi, "")
            display_text = f"{idx + 1}"
            if group_label:
                display_text = f"[{group_label}] {idx + 1}"

            btn = ctk.CTkButton(
                frame, text=display_text, image=photo,
                width=self.TW + 20, height=self.TH + 16,
                compound="top", font=ctk.CTkFont(size=10),
                fg_color=("gray80", "gray30") if idx == self.selected_idx else "transparent",
                command=lambda i=idx: self._select_page(i))
            btn.pack()

            if len(self.pages) > 1:
                ctk.CTkButton(
                    frame, text="x", width=18, height=18,
                    font=ctk.CTkFont(size=9), corner_radius=9,
                    fg_color="#c04040", hover_color="#a03030",
                    command=lambda i=idx: self._delete_page(i)
                ).place(relx=1.0, rely=0.0, anchor="ne")

    def _select_page(self, idx):
        self.selected_idx = idx
        self._refresh_thumbs()
        self._update_preview()

    def _delete_page(self, idx):
        page_num = self.pages[idx]
        self.pages.pop(idx)
        try:
            os.remove(self._cache_path(page_num))
        except OSError:
            pass
        new_splits = set()
        for sp in self.splits:
            if sp == idx:
                continue
            elif sp > idx:
                new_splits.add(sp - 1)
            else:
                new_splits.add(sp)
        self.splits = new_splits
        self.selected_idx = min(self.selected_idx, len(self.pages) - 1)
        self._refresh_thumbs()
        self._update_preview()

    def _insert_split(self):
        idx = self.selected_idx
        if idx >= len(self.pages) - 1:
            return
        self.splits.add(idx)
        self._refresh_thumbs()

    def _remove_split(self):
        idx = self.selected_idx
        if idx in self.splits:
            self.splits.discard(idx)
            self._refresh_thumbs()

    def _rename_group(self):
        groups = self._get_groups()
        gi = None
        for i, g in enumerate(groups):
            if self.selected_idx in g:
                gi = i
                break
        if gi is None:
            return

        pop = ctk.CTkToplevel(self)
        pop.title("重命名分组")
        pop.geometry("300x140")
        pop.resizable(False, False)
        pop.transient(self)
        pop.grab_set()

        ctk.CTkLabel(pop, text=f"分组 {gi + 1} 名称",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(12, 4))
        entry = ctk.CTkEntry(pop, width=260)
        entry.pack(pady=4)
        current_name = self.group_names.get(gi, "")
        if current_name:
            entry.insert(0, current_name)
            entry.select_range(0, "end")
        else:
            entry.configure(placeholder_text=f"文档_{gi + 1}")
        entry.focus_set()

        def confirm():
            name = entry.get().strip()
            if name:
                self.group_names[gi] = name
            elif gi in self.group_names:
                del self.group_names[gi]
            pop.destroy()
            self._refresh_thumbs()

        ctk.CTkButton(pop, text="确认", command=confirm).pack(pady=4)
        entry.bind("<Return>", lambda e: confirm())

    def _on_resize(self, event=None):
        if event is None:
            return
        w, h = event.width, event.height
        if abs(w - self._last_size[0]) < 30 and abs(h - self._last_size[1]) < 30:
            return
        self._last_size = (w, h)
        self._update_preview()

    def _update_preview(self):
        try:
            page_num = self.pages[self.selected_idx]
            img = Image.open(self._cache_path(page_num))
            w = max(200, self.winfo_width() - 60)
            h = max(150, self.winfo_height() - 300)
            img.thumbnail((w, h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._preview_photo = photo
            self.img_lbl.configure(image=photo, text="")
            self.page_info_lbl.configure(
                text=f"第 {self.selected_idx + 1} 页 / 共 {len(self.pages)} 页")
        except Exception as e:
            self.img_lbl.configure(image=None, text=f"预览失败: {e}")

    def _confirm(self):
        default_dir = os.path.dirname(self.output_path)
        default_name = os.path.basename(self.output_path)
        out_ext = default_name.rsplit(".", 1)[-1].lower()

        _filters = [
            ("JPEG 图片", "*.jpg *.jpeg"),
            ("PNG 图片", "*.png"),
            ("TIFF 图片", "*.tiff *.tif"),
            ("PDF 文档", "*.pdf"),
        ]
        ext_to_filter = {"jpg": 0, "jpeg": 0, "png": 1, "tiff": 2, "tif": 2, "pdf": 3}
        idx = ext_to_filter.get(out_ext, 0)
        _filters.insert(0, _filters.pop(idx))

        save_path = filedialog.asksaveasfilename(
            parent=self, title="保存扫描文件（选择基础名称）",
            initialdir=default_dir, initialfile=default_name,
            defaultextension=f".{out_ext}", filetypes=_filters)
        if not save_path:
            return

        save_dir = os.path.dirname(save_path)
        save_stem = os.path.basename(save_path).rsplit(".", 1)[0]
        out_ext = save_path.rsplit(".", 1)[-1].lower()
        base_path = os.path.join(save_dir, save_stem)

        saved_files = []
        is_pdf = (out_ext == "pdf")
        groups = self._get_groups()
        has_groups = len(groups) > 1

        for gi, group_indices in enumerate(groups):
            group_name = self.group_names.get(gi, "")
            if has_groups:
                prefix = group_name if group_name else f"文档_{gi + 1}"
            else:
                prefix = None

            if is_pdf:
                pdf_images = []
                for page_idx in group_indices:
                    page_num = self.pages[page_idx]
                    try:
                        img = Image.open(self._cache_path(page_num))
                        pdf_images.append(img.convert("RGB"))
                    except Exception:
                        pass
                if pdf_images:
                    if prefix:
                        pdf_path = f"{base_path}_{prefix}.pdf"
                    else:
                        pdf_path = f"{base_path}.pdf"
                    pdf_images[0].save(pdf_path, "PDF", save_all=True,
                                        append_images=pdf_images[1:])
                    saved_files.append(pdf_path)
            else:
                for seq, page_idx in enumerate(group_indices, 1):
                    page_num = self.pages[page_idx]
                    try:
                        cache_file = self._cache_path(page_num)
                        if prefix:
                            fpath = f"{base_path}_{prefix}_{seq:03d}.{out_ext}"
                        else:
                            fpath = f"{base_path}_{seq:03d}.{out_ext}"
                        if out_ext in ("jpg", "jpeg"):
                            shutil.copy2(cache_file, fpath)
                        else:
                            img = Image.open(cache_file)
                            if out_ext == "png":
                                img.save(fpath, "PNG")
                            elif out_ext in ("tiff", "tif"):
                                img.save(fpath, "TIFF")
                            else:
                                if img.mode != "RGB":
                                    img = img.convert("RGB")
                                img.save(fpath, "JPEG", quality=95)
                        saved_files.append(fpath)
                    except Exception:
                        pass

        cache_manager.remove_job(self.job_id)

        if saved_files:
            history_manager.append({
                "device_name": self.device_name,
                "device_ip": self.device_ip,
                "page_count": len(self.pages),
                "format": self.ext,
                "file_path": saved_files[0],
                "file_count": len(saved_files),
            })

        self.destroy()

        if saved_files:
            os.startfile(os.path.dirname(saved_files[0]))

    def _cancel(self):
        cache_manager.remove_job(self.job_id)
        self.destroy()


# ================================================
#  单个扫描仪卡片
# ================================================

class ScannerCard(ctk.CTkFrame):
    """高对比度、可选中、支持自定义名称的扫描仪卡片"""

    def __init__(self, parent, scanner: ScannerInfo, idx: int, app: ScanApp):
        super().__init__(parent, corner_radius=10, border_width=1)
        self.grid_columnconfigure(0, weight=1)

        self.scanner = scanner
        self.idx = idx
        self.app = app
        self._is_selected = False

        self._build()

    def _build(self):
        s = self.scanner

        # 名称行 + 重命名按钮
        name_frame = ctk.CTkFrame(self, fg_color="transparent")
        name_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        name_frame.grid_columnconfigure(0, weight=1)

        name_text = _label_for(s)
        self.name_lbl = ctk.CTkLabel(
            name_frame, text=name_text,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w", wraplength=220)
        self.name_lbl.grid(row=0, column=0, sticky="w")

        self.rename_btn = ctk.CTkButton(
            name_frame, text="✏", width=28, height=24,
            font=ctk.CTkFont(size=11),
            fg_color="transparent",
            text_color=("gray45", "gray65"),
            hover_color=("gray80", "gray30"),
            command=self._show_rename_popup)
        self.rename_btn.grid(row=0, column=1, padx=(4, 0))

        # 上移/下移按钮（调整优先级）
        self.up_btn = ctk.CTkButton(
            name_frame, text="▲", width=28, height=24,
            font=ctk.CTkFont(size=10),
            fg_color="transparent",
            text_color=("gray45", "gray65"),
            hover_color=("gray80", "gray30"),
            command=self._move_up)
        self.up_btn.grid(row=0, column=2, padx=(2, 0))

        self.down_btn = ctk.CTkButton(
            name_frame, text="▼", width=28, height=24,
            font=ctk.CTkFont(size=10),
            fg_color="transparent",
            text_color=("gray45", "gray65"),
            hover_color=("gray80", "gray30"),
            command=self._move_down)
        self.down_btn.grid(row=0, column=3, padx=(2, 0))

        # 信息行
        info_parts = [f"IP {s.ip}"]
        if s.model:
            info_parts.append(f"{s.model}")
        self.info_lbl = ctk.CTkLabel(
            self, text="  |  ".join(info_parts),
            font=ctk.CTkFont(size=11),
            text_color=("gray45", "gray65"),
            anchor="w")
        self.info_lbl.grid(row=1, column=0, sticky="w", padx=12)

        # 标签行
        tag_row = ctk.CTkFrame(self, fg_color="transparent")
        tag_row.grid(row=2, column=0, sticky="w", padx=12, pady=(4, 10))

        # eSCL 标签
        escl_ok = bool(s.escl_url)
        self._tag(tag_row,
                  "eSCL" if escl_ok else "?eSCL",
                  "#1b7c6e" if escl_ok else "#c25a3e")

        # 扫描来源
        if s.has_adf:
            self._tag(tag_row, "ADF", "#1b6e8a")
        elif escl_ok:
            self._tag(tag_row, "平板", "#3d6e8a")
        else:
            self._tag(tag_row, "未知", "#5a5a5a")

        # 分辨率
        if s.resolutions:
            self._tag(tag_row, f"{max(s.resolutions)}DPI", "#4d5a6e")

        # 点击选中
        self.bind("<Button-1>", self._on_click)
        self.name_lbl.bind("<Button-1>", self._on_click)
        self.info_lbl.bind("<Button-1>", self._on_click)

        # 悬停效果
        for w in (self, self.name_lbl, self.info_lbl):
            w.bind("<Enter>", self._on_hover_enter)
            w.bind("<Leave>", self._on_hover_leave)

        # 双击扫描
        self.bind("<Double-Button-1>", lambda e: self.app._start_scan())

        # 右键改名
        self.bind("<Button-3>", self._on_right_click)
        self.name_lbl.bind("<Button-3>", self._on_right_click)
        self.info_lbl.bind("<Button-3>", self._on_right_click)

        # 初始样式
        self.set_selected(False)

    def _tag(self, parent, text, bg):
        """创建高对比度小标签"""
        tag = ctk.CTkFrame(parent, fg_color=bg, corner_radius=6)
        tag.pack(side="left", padx=3)
        ctk.CTkLabel(tag, text=text,
                     font=ctk.CTkFont(size=10, weight="bold"),
                     text_color="white").pack(padx=8, pady=3)

    def _on_click(self, event=None):
        self.app.select_scanner(self.idx)

    def _on_right_click(self, event=None):
        self._show_rename_popup()

    def _on_hover_enter(self, event=None):
        if not self._is_selected:
            self.configure(border_color=("gray65", "gray50"))

    def _on_hover_leave(self, event=None):
        if not self._is_selected:
            self.configure(border_color=("gray55", "gray40"))

    def set_selected(self, selected: bool):
        self._is_selected = selected
        if selected:
            self.configure(border_color=("#3B8ED0", "#1F6AA5"), border_width=2)
        else:
            self.configure(border_color=("gray55", "gray40"), border_width=1)

    def _show_rename_popup(self):
        pop = ctk.CTkToplevel(self)
        pop.title("重命名")
        pop.geometry("320x150")
        pop.resizable(False, False)
        pop.transient(self.app)
        pop.grab_set()

        ctk.CTkLabel(pop, text="自定义打印机名称",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(12, 4))
        ctk.CTkLabel(pop, text="留空则恢复默认名称",
                     font=ctk.CTkFont(size=11),
                     text_color=("gray45", "gray65")).pack()

        entry = ctk.CTkEntry(pop, width=280)
        entry.pack(pady=6)
        current = self.scanner.custom_name or self.scanner.display_name
        if not self.scanner.custom_name:
            entry.insert(0, "")
            entry.configure(placeholder_text=current)
        else:
            entry.insert(0, current)
            entry.select_range(0, "end")

        def confirm():
            self.app.set_custom_name(self.idx, entry.get())
            pop.destroy()

        ctk.CTkButton(pop, text="确认", command=confirm).pack(pady=4)

        # 居中到主窗口
        pop.update_idletasks()
        pw = self.app.winfo_width()
        ph = self.app.winfo_height()
        px = self.app.winfo_x()
        py = self.app.winfo_y()
        pop.geometry(f"+{px + (pw - 320) // 2}+{py + (ph - 150) // 2}")

        entry.focus_set()
        entry.bind("<Return>", lambda e: confirm())

    def _move_up(self):
        self.app.move_scanner(self.idx, -1)

    def _move_down(self):
        self.app.move_scanner(self.idx, 1)


# ================================================
#  扫描历史对话框
# ================================================

class HistoryDialog(ctk.CTkToplevel):
    """显示扫描历史记录，双击可打开文件或所在文件夹"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("扫描历史")
        self.geometry("700x500")
        self.transient(parent)
        self.grab_set()
        try:
            _p = tk.PhotoImage(file=get_resource_path("icon_64.png"))
            self.iconphoto(True, _p)
            self._icon_photo = _p
        except Exception:
            try:
                self.iconbitmap(get_resource_path("app.ico"))
            except Exception:
                pass

        self._build()
        self._load_records()

        # 居中
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 700) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 500) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _build(self):
        # 标题
        ctk.CTkLabel(self, text="扫描历史记录",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(12, 8))

        # 列表区域
        list_frame = ctk.CTkFrame(self)
        list_frame.pack(fill="both", expand=True, padx=14, pady=4)

        # 使用 Treeview 显示列表
        columns = ("time", "device", "pages", "format", "path")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=15)
        self.tree.heading("time", text="时间")
        self.tree.heading("device", text="设备")
        self.tree.heading("pages", text="页数")
        self.tree.heading("format", text="格式")
        self.tree.heading("path", text="文件路径")

        self.tree.column("time", width=140)
        self.tree.column("device", width=120)
        self.tree.column("pages", width=50)
        self.tree.column("format", width=60)
        self.tree.column("path", width=280)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 双击打开文件
        self.tree.bind("<Double-1>", self._on_double_click)

        # 按钮行
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(fill="x", padx=14, pady=(8, 14))

        ctk.CTkButton(bf, text="清空历史", width=100, height=32,
                      fg_color="transparent", border_width=1,
                      text_color=("#c04040", "#e06060"),
                      border_color=("#c04040", "#e06060"),
                      hover_color=("#f0e0e0", "#3a2020"),
                      command=self._clear_history).pack(side="left")

        ctk.CTkButton(bf, text="打开文件夹", width=120, height=32,
                      command=self._open_folder).pack(side="right", padx=4)
        ctk.CTkButton(bf, text="打开文件", width=100, height=32,
                      command=self._open_file).pack(side="right", padx=4)
        ctk.CTkButton(bf, text="关闭", width=80, height=32,
                      command=self.destroy).pack(side="right", padx=4)

    def _load_records(self):
        records = history_manager.list_records(limit=200)
        for r in records:
            ts = r.get("timestamp", "")[:19].replace("T", " ")
            device = r.get("device_name", r.get("device_ip", "?"))
            pages = str(r.get("page_count", 1))
            fmt = r.get("format", "?").upper()
            path = r.get("file_path", "")
            self.tree.insert("", "end", values=(ts, device, pages, fmt, path))

    def _get_selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return None
        item = self.tree.item(sel[0])
        return item["values"][4]  # path column

    def _on_double_click(self, event):
        path = self._get_selected_path()
        if path and os.path.exists(path):
            os.startfile(path)

    def _open_file(self):
        path = self._get_selected_path()
        if path and os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showinfo("提示", "文件不存在或已被移动")

    def _open_folder(self):
        path = self._get_selected_path()
        if path:
            folder = os.path.dirname(path)
            if os.path.exists(folder):
                os.startfile(folder)
            else:
                messagebox.showinfo("提示", "文件夹不存在")

    def _clear_history(self):
        if messagebox.askyesno("确认", "确定要清空所有扫描历史记录吗？"):
            history_manager.clear()
            for item in self.tree.get_children():
                self.tree.delete(item)


# ================================================

def main():
    # ★ 高 DPI 感知：防止 Windows 对窗口图标做位图拉伸导致模糊
    # 这是任务栏图标模糊的根本原因（CPython issue #119174）
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    
    # ★ Windows 任务栏图标：必须在创建任何窗口之前设置 AppUserModelID
    # 否则任务栏会显示 python.exe 的图标，且各阶段图标会闪烁变化
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HP.ScanTool.v3.3")
    except Exception:
        pass

    app = ScanApp()
    app.mainloop()
    # 兜底：mainloop 退出后强制终止进程
    os._exit(0)


if __name__ == "__main__":
    main()
