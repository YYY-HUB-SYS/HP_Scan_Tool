"""
惠普集成扫描工具 v3.2 — CustomTkinter 重写版
功能: 主题联动 / 高对比度配色 / 自定义名称 / 交互反馈 / 曝光控制(关闭·自动·手动)
"""

import io
import json
import os
import sys
import threading
import traceback
from datetime import datetime
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import customtkinter as ctk
from escl_engine import (
    discover_scanners, probe_escl, fetch_capabilities,
    get_scanner_status, execute_scan, auto_exposure, manual_exposure,
    ScannerInfo,
)

# ---------- 配置 ----------
def _app_dir():
    # PyInstaller 单文件 EXE：用 EXE 所在目录，避免 tmp 被清理
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(_app_dir(), "scan_config.json")
DEFAULT_OUT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "HP_Scans")

COLOR_MODE_MAP = {"彩色": "RGB24", "灰度": "Grayscale8", "黑白": "BlackAndWhite1"}

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


# ================================================
#  主窗口
# ================================================

class ScanApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.cfg = load_config()

        self.title("惠普集成扫描工具")
        self.geometry("860x720")
        self.minsize(740, 600)

        saved_theme = self.cfg.get("theme", "dark")
        ctk.set_appearance_mode(saved_theme)
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.scanners: list[ScannerInfo] = []
        self.selected: ScannerInfo | None = None
        self.scanning = False
        self.selected_idx: int = -1
        self._scan_cancel = threading.Event()

        self.output_dir = self.cfg.get("output_dir", DEFAULT_OUT_DIR)
        self.resolution_val = self.cfg.get("resolution", 300)
        self.color_mode_val = self.cfg.get("color_mode_ui", "彩色")
        self.output_format_val = self.cfg.get("output_format", "jpg")
        self.source_val = self.cfg.get("source", "平板")
        self.exposure_mode = self.cfg.get("exposure_mode", "关闭")
        self.brightness_val = self.cfg.get("brightness", 0)
        self.contrast_val = self.cfg.get("contrast", 0)

        self._build_topbar()
        self._build_main()
        self._build_progress()

        self._restore_presets()
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
                                       height=30, command=self._refresh,
                                       hover_color=("#3a7ebf", "#1f538d"))
        self.disc_btn.pack(side="left", padx=2)
        ctk.CTkButton(btn_bar, text="手动添加", height=30,
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

        # 状态条
        self.status_bar = ctk.CTkLabel(
            right, text="就绪 — 正在搜索打印机...",
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=("gray90", "gray17"),
            corner_radius=8, anchor="w", padx=12, pady=8)
        self.status_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))

        # 参数区标题
        ctk.CTkLabel(right, text="扫描参数",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=1, column=0, sticky="w", padx=12, pady=(4, 0))

        # 参数区
        params = ctk.CTkFrame(right)
        params.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
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

        # 曝光控制面板
        exp_frame = ctk.CTkFrame(right)
        exp_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 2))
        exp_frame.grid_columnconfigure(0, weight=1)

        exp_hdr = ctk.CTkFrame(exp_frame, fg_color="transparent")
        exp_hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        ctk.CTkLabel(exp_hdr, text="曝光调整",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")

        self.exp_mode_var = ctk.StringVar(value=self.exposure_mode)
        exp_seg = ctk.CTkSegmentedButton(
            exp_frame,
            values=["关闭", "自动", "手动"],
            variable=self.exp_mode_var,
            command=self._on_exposure_mode,
            height=28,
        )
        exp_seg.grid(row=1, column=0, padx=10, pady=(2, 4))

        # 手动曝光：亮度 / 对比度
        self.manual_frame = ctk.CTkFrame(exp_frame, fg_color="transparent")
        self.manual_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))
        self.manual_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.manual_frame, text="亮度",
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0, padx=(0, 4), sticky="w")
        self.bri_slider = ctk.CTkSlider(
            self.manual_frame, from_=-100, to=100, number_of_steps=200,
            command=self._on_brightness)
        self.bri_slider.grid(row=0, column=1, sticky="ew", padx=4)
        self.bri_lbl = ctk.CTkLabel(self.manual_frame, text=str(self.brightness_val),
                                    font=ctk.CTkFont(size=11), width=32)
        self.bri_lbl.grid(row=0, column=2, padx=(4, 0))
        self.bri_slider.set(self.brightness_val)

        ctk.CTkLabel(self.manual_frame, text="对比度",
                     font=ctk.CTkFont(size=11)).grid(row=1, column=0, padx=(0, 4), sticky="w", pady=(4, 0))
        self.con_slider = ctk.CTkSlider(
            self.manual_frame, from_=-100, to=100, number_of_steps=200,
            command=self._on_contrast)
        self.con_slider.grid(row=1, column=1, sticky="ew", padx=4, pady=(4, 0))
        self.con_lbl = ctk.CTkLabel(self.manual_frame, text=str(self.contrast_val),
                                    font=ctk.CTkFont(size=11), width=32)
        self.con_lbl.grid(row=1, column=2, padx=(4, 0), pady=(4, 0))
        self.con_slider.set(self.contrast_val)

        # 初始滑块状态
        self._set_manual_sliders(self.exposure_mode == "手动")

        # 保存目录
        save_row = ctk.CTkFrame(right, fg_color="transparent")
        save_row.grid(row=4, column=0, sticky="ew", padx=10, pady=6)
        ctk.CTkLabel(save_row, text="保存到",
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self.dir_entry = ctk.CTkEntry(save_row)
        self.dir_entry.pack(side="left", padx=6, fill="x", expand=True)
        self.dir_entry.insert(0, self.output_dir)
        ctk.CTkButton(save_row, text="浏览", width=56, height=28,
                      fg_color="transparent", border_width=1,
                      text_color=("gray30", "gray80"),
                      border_color=("gray40", "gray60"),
                      command=self._browse).pack(side="left")

        # 操作按钮行
        action = ctk.CTkFrame(right, fg_color="transparent")
        action.grid(row=5, column=0, sticky="ew", padx=10, pady=(8, 10))

        self.caps_btn = ctk.CTkButton(action, text="查询能力",
                                       height=32, border_width=1,
                                       text_color=("gray30", "gray80"),
                                       border_color=("gray40", "gray60"),
                                       fg_color="transparent",
                                       hover_color=("gray82", "gray25"),
                                       command=self._query_caps)
        self.caps_btn.pack(side="left", padx=3)

        self.stat_btn = ctk.CTkButton(action, text="预览状态",
                                       height=32, border_width=1,
                                       text_color=("gray30", "gray80"),
                                       border_color=("gray40", "gray60"),
                                       fg_color="transparent",
                                       hover_color=("gray82", "gray25"),
                                       command=self._check_status)
        self.stat_btn.pack(side="left", padx=3)

        self.scan_btn = ctk.CTkButton(action, text="扫描",
                                       height=40, width=160,
                                       font=ctk.CTkFont(size=15, weight="bold"),
                                       corner_radius=8,
                                       command=self._start_scan)
        self.scan_btn.pack(side="right", padx=3)

    def _param(self, parent, label, values, default, row, col, command=None):
        ctk.CTkLabel(parent, text=label,
                     font=ctk.CTkFont(size=12)).grid(row=0, column=col, padx=8, pady=(10, 0), sticky="w")
        cb = ctk.CTkComboBox(parent, values=values, state="readonly", width=110, command=command)
        cb.set(default)
        cb.grid(row=1, column=col, padx=8, pady=(2, 10))

    # ────────── 曝光控制 ──────────
    def _on_exposure_mode(self, value):
        self.exposure_mode = value
        self.cfg["exposure_mode"] = value
        save_config(self.cfg)
        self._set_manual_sliders(value == "手动")

    def _on_brightness(self, val):
        v = int(val)
        self.brightness_val = v
        self.bri_lbl.configure(text=str(v))

    def _on_contrast(self, val):
        v = int(val)
        self.contrast_val = v
        self.con_lbl.configure(text=str(v))

    def _set_manual_sliders(self, enabled):
        state = "normal" if enabled else "disabled"
        self.bri_slider.configure(state=state)
        self.con_slider.configure(state=state)

    # ────────── 窗口关闭清理 ──────────
    def destroy(self):
        self._scan_cancel.set()
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

    # ────────── 扫描仪卡片 ──────────
    def _rebuild_cards(self):
        for c in self.cards:
            c.destroy()
        self.cards.clear()

        for i, s in enumerate(self.scanners):
            card = ScannerCard(self.card_container, s, i, self)
            card.grid(row=i, column=0, sticky="ew", pady=3)
            self.cards.append(card)

        # 恢复选中状态高亮
        if 0 <= self.selected_idx < len(self.cards):
            self.cards[self.selected_idx].set_selected(True)

        # 更新设备计数
        n = len(self.scanners)
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
            self.scanners.append(si)

        self._rebuild_cards()
        if self.scanners:
            self.status_bar.configure(text=f"已加载 {len(self.scanners)} 台历史打印机")
            # 后台异步探活，不阻塞 UI
            threading.Thread(target=self._probe_saved, daemon=True).start()

    def _probe_saved(self):
        """后台逐台探活已加载的打印机（构造新对象后原子替换，避免半更新状态）"""
        for i, s in enumerate(list(self.scanners)):
            url = probe_escl(s.ip, timeout=3.0)
            if url:
                # 构造新的 ScannerInfo，复制所有字段后查询能力
                import copy
                new_s = copy.copy(s)
                new_s.escl_url = url
                fetch_capabilities(new_s, timeout=3.0)
                # 原子替换列表中的引用
                for j, sc in enumerate(self.scanners):
                    if sc.ip == s.ip:
                        self.scanners[j] = new_s
                        if self.selected and self.selected.ip == s.ip:
                            self.selected = new_s
                        break
            self.after(0, self._rebuild_cards)

    def _auto_discover(self):
        # 已有历史打印机则跳过自动搜索，直接加载
        if self.scanners:
            n = len(self.scanners)
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
        discovered = discover_scanners(timeout=4.0)
        existing_ips = {s.ip for s in self.scanners if s.ip}
        new_count = 0
        for d in discovered:
            if d.ip not in existing_ips:
                d.escl_url = probe_escl(d.ip, timeout=3.0) or ""
                if d.escl_url:
                    fetch_capabilities(d, timeout=3.0)
                self.scanners.append(d)
                existing_ips.add(d.ip)
                new_count += 1

        nicknames = self.cfg.get("nicknames", {})
        ips = []
        for s in self.scanners:
            if s.ip:
                ips.append({"ip": s.ip, "model": s.model})
                if s.custom_name:
                    nicknames[s.ip] = s.custom_name
        self.cfg["saved_ips"] = ips
        self.cfg["nicknames"] = nicknames
        save_config(self.cfg)

        self.after(0, lambda: self._on_discover_done(new_count))

    def _on_discover_done(self, new_count):
        self._set_loading(False)
        self._rebuild_cards()
        n = len(self.scanners)
        self.status_bar.configure(
            text=f"发现 {n} 台打印机 (+{new_count} 新增) — 单击选中后按「扫描」" if n
            else "未发现打印机，请确认电源和网络，或手动添加 IP")
        # 仅在无新设备时保留原选中；有新设备加入时重置以便用户看到完整列表
        if new_count > 0:
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

    # ────────── 选择 ──────────
    def select_scanner(self, idx: int):
        if 0 <= idx < len(self.scanners):
            self.selected = self.scanners[idx]
            self.selected_idx = idx

            sources = ["平板"]
            if self.selected.has_adf:
                sources.append("输稿器(ADF)")
            self.src_cb.configure(values=sources)
            # 恢复上次选择，但仅在选项可用时
            if self.source_val in sources:
                self.src_cb.set(self.source_val)
            else:
                self.src_cb.set(sources[0])
                self.source_val = sources[0]

            label = _label_for(self.selected)
            self.status_bar.configure(text=f"已选择: {label}")

            for i, card in enumerate(self.cards):
                card.set_selected(i == idx)

    # ────────── 自定义名称 ──────────
    def set_custom_name(self, idx: int, name: str):
        if 0 <= idx < len(self.scanners):
            self.scanners[idx].custom_name = name.strip() or ""
            nicknames = self.cfg.get("nicknames", {})
            ip = self.scanners[idx].ip
            if self.scanners[idx].custom_name:
                nicknames[ip] = self.scanners[idx].custom_name
            else:
                nicknames.pop(ip, None)
            self.cfg["nicknames"] = nicknames
            save_config(self.cfg)

            self._rebuild_cards()
            if self.selected_idx == idx:
                label = _label_for(self.scanners[idx])
                self.status_bar.configure(text=f"已选择: {label}")

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

        def probe():
            ip = ip_var.get().strip()
            if not ip:
                return
            try:
                port = int(port_var.get().strip() or "80")
            except ValueError:
                port = 80
            result_lbl.configure(text="探测中...", text_color=("gray45", "gray65"))
            add_btn = dlg.winfo_children()[-1]  # 获取"探测并添加"按钮
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
                    self.scanners.append(s)
                    self._rebuild_cards()
                    self.status_bar.configure(text=f"已添加 {_label_for(s)}")
                    result_lbl.configure(text="成功 — eSCL 可用", text_color="#2a9d8f")
                    dlg.after(800, dlg.destroy)
                else:
                    result_lbl.configure(text="该 IP 不支持 eSCL，请确认打印机已启用 AirScan",
                                         text_color="#e07b5a")

            threading.Thread(target=do_probe, daemon=True).start()

        result_lbl.pack()
        ctk.CTkButton(dlg, text="探测并添加", width=140, command=probe).pack(pady=8)

    # ────────── 能力查询 ──────────
    def _query_caps(self):
        if not self.selected:
            messagebox.showwarning("提示", "请先选择一台打印机")
            return
        if not self.selected.escl_url:
            messagebox.showwarning("提示", "该打印机未配置 eSCL URL")
            return

        self._set_loading(True)
        scanner_ref = self.selected  # 捕获当前引用，避免线程中读取到已切换的打印机

        def do():
            s = fetch_capabilities(scanner_ref, timeout=5.0)
            for i, sc in enumerate(self.scanners):
                if sc.ip == s.ip:
                    self.scanners[i] = s
                    break
            self.after(0, lambda: self._caps_done(s))

        threading.Thread(target=do, daemon=True).start()

    def _caps_done(self, s):
        self._set_loading(False)
        self._rebuild_cards()

        sources = ["平板"]
        if s.has_adf:
            sources.append("输稿器(ADF)")
        self.src_cb.configure(values=sources)
        if self.source_val in sources:
            self.src_cb.set(self.source_val)
        else:
            self.src_cb.set(sources[0])
            self.source_val = sources[0]

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

        def do():
            st = get_scanner_status(url, timeout=4.0)
            self.after(0, lambda: messagebox.showinfo(
                "打印机状态",
                f"状态: {st.get('state', '未知')}\nADF 有纸: {'是' if st.get('adf_loaded') else '否'}"
            ))

        threading.Thread(target=do, daemon=True).start()

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
        if self.scanning:
            return
        if not self.selected:
            messagebox.showwarning("提示", "请先选择一台打印机")
            return

        self._scan_cancel.clear()  # 重置取消标志
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

        self.scanning = True
        self.scan_btn.configure(text="扫描中...", state="disabled")
        self._set_loading(True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_tag = self.selected.model.replace(" ", "_") if self.selected.model else "scan"
        fname = f"HP_{model_tag}_{ts}.{self.output_format_val}"
        fpath = os.path.join(out_dir, fname)

        self.cfg.update({
            "resolution": self.resolution_val,
            "color_mode_ui": self.color_mode_val,
            "output_format": self.output_format_val,
            "output_dir": out_dir,
            "source": self.src_cb.get(),
            "exposure_mode": self.exposure_mode,
            "brightness": self.brightness_val,
            "contrast": self.contrast_val,
        })
        save_config(self.cfg)

        source_val = "Platen" if "平板" in self.src_cb.get() else "Feeder"
        self.source_val = self.src_cb.get()

        threading.Thread(target=self._do_capture, args=(
            self.selected, fpath, source_val,
        ), daemon=True).start()

    def _do_capture(self, scanner, output_path, source_val):
        """扫描到内存，然后弹出预览对话框"""
        try:
            if self._scan_cancel.is_set():
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
                except Exception:
                    pass

            self.after(0, lambda: self.status_bar.configure(text="正在扫描..."))
            data, ext = execute_scan(
                scanner=scanner,
                resolution=self.resolution_val,
                color_mode=COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
                output_format=self.output_format_val,
                source=source_val,
                timeout=90.0,
            )
            self.after(0, lambda: self._show_preview(data, ext, output_path))
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

        self.scanning = True
        self.scan_btn.configure(text="WIA...", state="disabled")
        self._set_loading(True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fpath = os.path.join(out_dir, f"WIA_scan_{ts}.{self.output_format_val}")

        def do():
            from wia_engine import try_wia_scan
            result = try_wia_scan(
                output_path=fpath,
                resolution=self.resolution_val,
                color_mode_name=COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
                output_format=self.output_format_val,
            )
            self.after(0, lambda: self._wia_done(result is not None, result or "WIA 扫描失败"))

        threading.Thread(target=do, daemon=True).start()

    def _wia_done(self, success, result):
        self.scanning = False
        self.scan_btn.configure(text="扫描", state="normal")
        self._set_loading(False)
        if success:
            self.status_bar.configure(text="扫描完成")
            if messagebox.askyesno("扫描完成", f"已保存:\n{result}\n\n打开所在文件夹？"):
                os.startfile(os.path.dirname(result))
        else:
            self.status_bar.configure(text="扫描失败")
            messagebox.showerror("扫描失败", result)

    def _reset_scan_ui(self):
        self.scanning = False
        self.scan_btn.configure(text="扫描", state="normal")
        self._set_loading(False)

    def _scan_error(self, msg):
        self._reset_scan_ui()
        self.status_bar.configure(text="扫描失败")
        messagebox.showerror("扫描失败", f"{msg}\n\n如果 eSCL 不通，请确认:\n"
                            "1. 打印机已启用 eSCL/AirScan\n"
                            "2. 浏览器访问 http://打印机IP/eSCL/ScannerStatus 确认可达\n"
                            "3. 防火墙未拦截")

    def _show_preview(self, data, ext, output_path):
        self._reset_scan_ui()
        self.status_bar.configure(text="扫描完成 — 调整曝光效果后点击保存")
        PreviewDialog(self, data, ext, output_path)


# ================================================
#  扫描预览对话框（实时曝光调整）
# ================================================

class PreviewDialog(ctk.CTkToplevel):
    """扫描后预览：实时调整曝光效果，确认后保存"""

    PW, PH = 420, 360  # 预览区域尺寸

    def __init__(self, parent, raw_data: bytes, ext: str, output_path: str):
        super().__init__(parent)
        self.title("扫描预览 — 调整曝光效果")
        self.geometry("540x640")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.raw = raw_data
        self.ext = ext
        self.output_path = output_path
        self.mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                     "png": "image/png", "tiff": "image/tiff",
                     "bmp": "image/bmp"}.get(ext.lower(), "image/jpeg")

        self.exp_mode = "关闭"
        self.bri = 0
        self.con = 0
        self._base_photo = None   # 缓存预览尺寸的原始图
        self._preview_photo = None

        self._build()
        self._update_preview()

        # 居中显示
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 540) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 640) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.protocol("WM_DELETE_WINDOW", self._cancel)

    # ────────── 构建 UI ──────────
    def _build(self):
        # 图片预览区
        pf = ctk.CTkFrame(self)
        pf.pack(fill="x", padx=14, pady=(14, 6))
        self.img_lbl = ctk.CTkLabel(pf, text="正在加载预览...",
                                    width=self.PW, height=self.PH,
                                    fg_color=("gray85", "gray25"),
                                    corner_radius=6)
        self.img_lbl.pack(padx=6, pady=6)

        # 曝光控制区
        ef = ctk.CTkFrame(self)
        ef.pack(fill="x", padx=14, pady=4)

        ctk.CTkLabel(ef, text="曝光模式",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=8, pady=(6, 2))

        self.exp_var = ctk.StringVar(value="关闭")
        ctk.CTkSegmentedButton(
            ef, values=["关闭", "自动", "手动"],
            variable=self.exp_var,
            command=self._on_mode, height=30,
        ).pack(padx=8, pady=(0, 6))

        # 手动滑块
        mf = ctk.CTkFrame(ef, fg_color="transparent")
        mf.pack(fill="x", padx=8, pady=(0, 6))
        mf.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(mf, text="亮度", font=ctk.CTkFont(size=11)).grid(
            row=0, column=0, sticky="w", padx=(0, 4))
        self.bri_s = ctk.CTkSlider(mf, from_=-100, to=100, number_of_steps=200,
                                   command=self._on_bri)
        self.bri_s.grid(row=0, column=1, sticky="ew", padx=4)
        self.bri_s.configure(state="disabled")
        self.bri_v = ctk.CTkLabel(mf, text="0", font=ctk.CTkFont(size=11), width=30)
        self.bri_v.grid(row=0, column=2, padx=(4, 0))

        ctk.CTkLabel(mf, text="对比度", font=ctk.CTkFont(size=11)).grid(
            row=1, column=0, sticky="w", padx=(0, 4), pady=(4, 0))
        self.con_s = ctk.CTkSlider(mf, from_=-100, to=100, number_of_steps=200,
                                   command=self._on_con)
        self.con_s.grid(row=1, column=1, sticky="ew", padx=4, pady=(4, 0))
        self.con_s.configure(state="disabled")
        self.con_v = ctk.CTkLabel(mf, text="0", font=ctk.CTkFont(size=11), width=30)
        self.con_v.grid(row=1, column=2, padx=(4, 0), pady=(4, 0))

        # 提示
        ctk.CTkLabel(ef, text="「自动」去灰底拉伸  |  「手动」微调亮度对比度",
                     font=ctk.CTkFont(size=10),
                     text_color=("gray50", "gray60")).pack(pady=(0, 6))

        # 按钮行
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(fill="x", padx=14, pady=(4, 14))

        ctk.CTkButton(bf, text="重新扫描", width=100, height=34,
                      fg_color="transparent", border_width=1,
                      text_color=("gray30", "gray80"),
                      border_color=("gray40", "gray60"),
                      command=self._cancel).pack(side="left")
        ctk.CTkButton(bf, text="保存", width=140, height=38,
                      font=ctk.CTkFont(size=14, weight="bold"),
                      command=self._confirm).pack(side="right")

    # ────────── 曝光模式切换 ──────────
    def _on_mode(self, value):
        self.exp_mode = value
        if value == "手动":
            self.bri_s.configure(state="normal")
            self.con_s.configure(state="normal")
        else:
            self.bri_s.configure(state="disabled")
            self.con_s.configure(state="disabled")
        self._update_preview()

    def _on_bri(self, val):
        self.bri = int(val)
        self.bri_v.configure(text=str(self.bri))
        self._update_preview()

    def _on_con(self, val):
        self.con = int(val)
        self.con_v.configure(text=str(self.con))
        self._update_preview()

    # ────────── 预览渲染 ──────────
    def _update_preview(self):
        """根据当前曝光设置实时刷新预览图"""
        try:
            img = Image.open(io.BytesIO(self.raw))

            # 应用曝光处理（全精度原图）
            if self.exp_mode == "自动":
                buf = io.BytesIO()
                img.save(buf, "JPEG" if img.mode == "RGB" else "PNG")
                processed = auto_exposure(buf.getvalue(), self.mime)
                img = Image.open(io.BytesIO(processed))
            elif self.exp_mode == "手动" and (self.bri != 0 or self.con != 0):
                buf = io.BytesIO()
                img.save(buf, "JPEG" if img.mode == "RGB" else "PNG")
                processed = manual_exposure(buf.getvalue(), self.bri, self.con, self.mime)
                img = Image.open(io.BytesIO(processed))

            # 缩放到预览尺寸
            img.thumbnail((self.PW, self.PH), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._preview_photo = photo
            self.img_lbl.configure(image=photo, text="")
        except Exception as e:
            self.img_lbl.configure(image=None, text=f"预览失败: {e}")

    # ────────── 确认保存 ──────────
    def _confirm(self):
        data = self.raw
        # 按最终选择的曝光模式处理全精度数据
        if self.exp_mode == "自动":
            data = auto_exposure(data, self.mime)
        elif self.exp_mode == "手动" and (self.bri != 0 or self.con != 0):
            data = manual_exposure(data, self.bri, self.con, self.mime)

        # 格式转换（如需要）
        out_ext = self.output_path.rsplit(".", 1)[-1].lower()
        if out_ext == "pdf" and self.ext != "pdf":
            img = Image.open(io.BytesIO(data))
            pdf_path = self.output_path.rsplit(".", 1)[0] + ".pdf"
            img.convert("RGB").save(pdf_path, "PDF")
            saved = pdf_path
        else:
            final = self.output_path
            if not final.lower().endswith(f".{self.ext}"):
                final = f"{final}.{self.ext}"
            with open(final, "wb") as f:
                f.write(data)
            saved = final

        self.destroy()
        if messagebox.askyesno("保存成功", f"已保存:\n{saved}\n\n打开所在文件夹？"):
            os.startfile(os.path.dirname(saved))

    def _cancel(self):
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

        # 名称行
        name_text = _label_for(s)
        self.name_lbl = ctk.CTkLabel(
            self, text=name_text,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w", wraplength=260)
        self.name_lbl.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))

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
        pop.transient(self)
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
        entry.focus_set()
        entry.bind("<Return>", lambda e: confirm())


# ================================================

def main():
    app = ScanApp()
    app.mainloop()
    # 兜底：mainloop 退出后强制终止进程
    os._exit(0)


if __name__ == "__main__":
    main()
