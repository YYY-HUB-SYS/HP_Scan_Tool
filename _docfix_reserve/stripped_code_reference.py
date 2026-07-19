"""
DocFix 剥离代码参考 — 从 HP Scan Tool 中剥离的图像后处理代码
============================================================
来源文件: hp_scan_gui.py, scan_coordinator.py, escl_engine.py, cli.py
剥离日期: 2026-07-19

本文件包含所有从 HP Scan Tool 中剥离的代码段，供 DocFix 项目复用。
每个代码段标注了来源文件和原始行号。
"""

# ============================================================
# 1. hp_scan_gui.py — 曝光模式按钮组工具函数 (原行 124-168)
# ============================================================

_MODE_COLORS = {
    "关闭": ("#6b6b6b", "#5a5a5a"),   # 灰色 - 无处理
    "自动": ("#2a7d4f", "#1e6b40"),   # 绿色 - 智能处理
    "手动": ("#c47a2a", "#a86520"),   # 橙色 - 手动控制
}


def _build_mode_buttons(parent, values, variable, command, height=30):
    """
    创建一组带颜色区分的模式按钮。
    选中: 实心填充 + 白色文字; 未选中: 透明 + 描边。
    返回: (buttons_dict, update_func)
    """
    container = ctk.CTkFrame(parent, fg_color="transparent")
    buttons = {}

    def _refresh():
        current = variable.get()
        for name, btn in buttons.items():
            if name == current:
                bg, _ = _MODE_COLORS.get(name, ("#6b6b6b", "#5a5a5a"))
                btn.configure(fg_color=bg, text_color="white", border_width=0)
            else:
                btn.configure(fg_color="transparent", text_color=("gray70", "gray80"),
                              border_width=1, border_color=("gray50", "gray60"))

    for i, name in enumerate(values):
        btn = ctk.CTkButton(
            container, text=name, width=70, height=height,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="transparent", border_width=1,
            text_color=("gray70", "gray80"),
            border_color=("gray50", "gray60"),
            hover_color=("gray80", "gray30"),
            command=lambda n=name: (variable.set(n), command(n) if command else None, _refresh()),
        )
        btn.grid(row=0, column=i, padx=2)
        buttons[name] = btn

    _refresh()
    return container, buttons, _refresh


# ============================================================
# 2. hp_scan_gui.py — ScanApp 曝光状态初始化 (原行 249-251)
# ============================================================
# 在 ScanApp.__init__ 中:
#   self.exposure_mode = self.cfg.get("exposure_mode", "关闭")
#   self.brightness_val = self.cfg.get("brightness", 0)
#   self.contrast_val = self.cfg.get("contrast", 0)


# ============================================================
# 3. hp_scan_gui.py — 曝光控制面板 UI (原行 377-425)
# ============================================================
# 在 ScanApp._build_main 中，右侧面板的曝光控制区域:

# 曝光控制面板（初始隐藏，扫描后才显示——有预览才能确定曝光）
self._exp_visible = False
exp_frame = ctk.CTkFrame(right)
self.exp_frame = exp_frame
exp_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 2))
exp_frame.grid_remove()  # 初始隐藏
exp_frame.grid_columnconfigure(0, weight=1)

exp_hdr = ctk.CTkFrame(exp_frame, fg_color="transparent")
exp_hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
ctk.CTkLabel(exp_hdr, text="曝光调整",
             font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")

self.exp_mode_var = ctk.StringVar(value=self.exposure_mode)
exp_mode_container, self._exp_mode_btns, self._exp_mode_refresh = _build_mode_buttons(
    exp_frame, ["关闭", "自动", "手动"],
    self.exp_mode_var, self._on_exposure_mode, height=28,
)
exp_mode_container.grid(row=1, column=0, padx=10, pady=(2, 4))

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


# ============================================================
# 4. hp_scan_gui.py — 曝光控制回调 (原行 499-520)
# ============================================================

def _on_exposure_mode(self, value):
    self.exposure_mode = value
    self.cfg["exposure_mode"] = value
    save_config(self.cfg)
    self._set_manual_sliders(value == "手动")
    self._exp_mode_refresh()

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


# ============================================================
# 5. hp_scan_gui.py — _show_exposure_panel (原行 1151-1155)
# ============================================================

def _show_exposure_panel(self):
    """扫描完成后显示曝光调整面板（首次扫描后展开）"""
    if not self._exp_visible:
        self._exp_visible = True
        self.exp_frame.grid()


# ============================================================
# 6. hp_scan_gui.py — PreviewDialog 完整类 (原行 1185-1641)
# ============================================================
# 整个 PreviewDialog 类都是图像后处理功能:
# - 实时曝光调整预览 (亮度/对比度/Gamma/阴影/高光/通道增益)
# - 直方图显示
# - 预设选择/保存
# - apply_exposure 调用
# 完整代码见 exposure.py 和 preset_manager.py (已单独保存)
# 关键方法:
#   __init__: 初始化曝光状态 (exp_mode, bri, con, gamma, shadows, highlights, r/g/b_gain)
#   _build: 构建直方图Canvas + 预设行 + 曝光控制区 (模式按钮 + 5个滑块)
#   _on_mode/_on_bri/_on_con/_on_gamma/_on_shadows/_on_highlights: 曝光回调
#   _on_preset_select/_on_preset_save: 预设加载/保存
#   _update_preview: 从缓存加载 → apply_exposure → 更新直方图 → 缩放显示
#   _update_histogram: 绘制 RGB/L 直方图
#   _confirm: 最终保存时调用 apply_exposure + 保存文件


# ============================================================
# 7. hp_scan_gui.py — MultiPagePreviewDialog 曝光部分 (原行 1647-2304)
# ============================================================
# 需要剥离的部分:
# - 曝光状态初始化 (原行 1687-1697):
#   self.exp_mode = "关闭"
#   self.bri = 0 / self.con = 0 / self.gamma = 1.0 / self.shadows = 0 / self.highlights = 0
#   self._per_page_overrides = {}
#   self._per_page_mode = False
#
# - 曝光控制 UI (原行 1799-1866):
#   模式按钮 + 逐页编辑按钮 + 亮度/对比度/Gamma/阴影/高光 5个滑块
#
# - 曝光控制回调 (原行 2086-2128):
#   _on_mode/_on_bri/_on_con/_on_gamma/_on_shadows/_on_highlights
#
# - 逐页覆盖逻辑 (原行 1940-2004):
#   _select_page 中的曝光保存/加载
#   _toggle_per_page/_save_current_as_override/_load_override_for_selected/_sync_sliders_from_global
#
# - 预览渲染中的曝光调用 (原行 2147-2167):
#   _update_preview 中调用 apply_exposure
#
# - _process_page (原行 2170-2186):
#   从缓存加载并应用曝光，支持逐页覆盖
#
# - _confirm 中的曝光处理 (原行 2224-2295):
#   调用 _process_page 应用曝光
#   记录曝光参数到历史

# 需要保留的部分 (扫描流程延伸):
# - 缩略图导航 (_refresh_thumbs, _select_page 的选中逻辑)
# - 页面删除 (_delete_page)
# - 分组管理 (_insert_split, _remove_split, _rename_group, _get_groups)
# - 保存文件对话框 + 格式转换 + 分组输出
# - 缓存清理 + 历史记录


# ============================================================
# 8. hp_scan_gui.py — 配置持久化中的曝光字段 (原行 989-991)
# ============================================================
# 在 _start_scan 中:
#   "exposure_mode": self.exposure_mode,
#   "brightness": self.brightness_val,
#   "contrast": self.contrast_val,


# ============================================================
# 9. scan_coordinator.py — 剥离的函数
# ============================================================

# 导入 (原行 23):
from exposure import apply_exposure

# process_image_with_exposure (原行 309-318):
def process_image_with_exposure(cache_path, mode, brightness=0, contrast=0,
                                 gamma=1.0, shadows=0, highlights=0,
                                 channel_gains=(1.0, 1.0, 1.0),
                                 mime="image/jpeg"):
    """从缓存加载图片并应用曝光处理，返回 PIL Image"""
    from PIL import Image
    img = Image.open(cache_path)
    return apply_exposure(img, mode=mode, brightness=brightness, contrast=contrast,
                          gamma=gamma, shadows=shadows, highlights=highlights,
                          channel_gains=channel_gains, mime=mime)

# save_scan_result (原行 321-336):
def save_scan_result(img, output_path, output_format="jpg"):
    """保存处理后的 PIL Image 到目标文件"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if output_format == "pdf":
        img.save(output_path, "PDF", resolution=150.0)
    elif output_format == "png":
        img.save(output_path, "PNG")
    elif output_format == "bmp":
        if img.mode == "RGB":
            img.save(output_path, "BMP")
        else:
            img.convert("RGB").save(output_path, "BMP")
    else:
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(output_path, "JPEG", quality=95)

# save_multipage_result (原行 372-421):
def save_multipage_result(job_id, ext, output_path, output_format, groups,
                           group_names, process_page_fn):
    """
    保存多页扫描结果。
    process_page_fn(page_idx) -> PIL.Image 用于获取处理后的页面图像。
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    base, _ = os.path.splitext(output_path)
    saved_paths = []

    if output_format == "pdf" and len(groups) == 1:
        # 单组 → 单个 PDF
        pages = [process_page_fn(p) for p in groups[0]]
        if pages:
            pages[0].save(output_path, "PDF", resolution=150.0,
                          save_all=True, append_images=pages[1:])
            saved_paths.append(output_path)
    elif output_format == "pdf":
        # 多组 → 多个 PDF
        for gi, group in enumerate(groups):
            pages = [process_page_fn(p) for p in group]
            if pages:
                name = group_names[gi] if gi < len(group_names) else f"组{gi + 1}"
                safe = name.replace("/", "_").replace("\\", "_")
                path = f"{base}_{safe}.pdf"
                pages[0].save(path, "PDF", resolution=150.0,
                              save_all=True, append_images=pages[1:])
                saved_paths.append(path)
    else:
        # 逐页保存为图片
        for gi, group in enumerate(groups):
            for pi in group:
                img = process_page_fn(pi)
                if len(groups) > 1:
                    name = group_names[gi] if gi < len(group_names) else f"组{gi + 1}"
                    safe = name.replace("/", "_").replace("\\", "_")
                    path = f"{base}_{safe}_p{pi + 1}.{ext}"
                else:
                    path = f"{base}_p{pi + 1}.{ext}"
                if output_format == "png":
                    img.save(path, "PNG")
                elif output_format == "bmp":
                    (img if img.mode == "RGB" else img.convert("RGB")).save(path, "BMP")
                else:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(path, "JPEG", quality=95)
                saved_paths.append(path)

    return saved_paths

# persist_scan_settings 中的曝光参数 (原行 289-303):
# 需要移除 exposure_mode, brightness, contrast 参数


# ============================================================
# 10. escl_engine.py — 剥离的代码
# ============================================================

# re-export (原行 333-337):
from exposure import (  # noqa: E402
    apply_exposure, auto_exposure, manual_exposure,
    _stretch_channel, _img_to_bytes,
)

# scan_to_file 中的曝光参数和调用 (原行 496-516):
# 函数签名中的 exposure_mode, brightness, contrast 参数
# 函数体中的:
#   mime = FORMAT_MIME.get(ext, "image/jpeg")
#   if exposure_mode == "auto":
#       data = auto_exposure(data, mime)
#   elif exposure_mode == "manual":
#       data = manual_exposure(data, brightness, contrast, mime)


# ============================================================
# 11. cli.py — 剥离的代码
# ============================================================

# cmd_scan 中的曝光处理 (原行 64-77):
exposure_mode = getattr(args, "exposure", "off")
if exposure_mode != "off":
    from PIL import Image as PILImage
    from exposure import apply_exposure
    from escl_engine import FORMAT_MIME
    mime = FORMAT_MIME.get(ext.lower(), "image/jpeg")
    img = PILImage.open(io.BytesIO(data))
    img = apply_exposure(img, mode=exposure_mode, mime=mime)
    buf = io.BytesIO()
    pil_fmt = "JPEG" if mime == "image/jpeg" else "PNG"
    img.save(buf, pil_fmt)
    data = buf.getvalue()
    print(f"已应用曝光模式: {exposure_mode}")

# 历史记录中的曝光字段 (原行 102):
"exposure_mode": exposure_mode,

# argparse 参数 (原行 236-237):
p_scan.add_argument("--exposure", default="off", choices=["off", "auto", "manual"],
                    help="曝光模式 (默认 off)")
