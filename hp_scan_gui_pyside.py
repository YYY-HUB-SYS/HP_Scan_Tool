"""
惠普集成扫描工具 v4.0 — PySide6 重写版（UI 优化）
设计系统: Soft UI Evolution + Micro-interactions
功能: 磁盘缓存 / 并发扫描 / 多页ADF / WSD发现 / CLI / 双面扫描 / 页面旋转 / 自动裁边 / 空白页检测 / 监听模式 / 多目标输出 / 配置文件
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
from datetime import datetime
from typing import Optional

import cache_manager
import history_manager
import profile_manager
from scan_coordinator import (
    ScanCoordinator, ScannerInfo, save_config, load_config,
    _label_for, _ip_label_for, DEFAULT_OUT_DIR,
    COLOR_MODE_MAP, SOURCE_MAP,
)
from escl_engine import auto_crop, is_blank_page

# ────────── PySide6 导入 ──────────
from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QSize, QRectF, QPointF,
    QPropertyAnimation, QEasingCurve, QParallelAnimationGroup,
    QAbstractAnimation, QPoint, QRect,
)
from PySide6.QtGui import (
    QPixmap, QImage, QIcon, QFont, QFontDatabase,
    QPainter, QColor, QPen, QBrush, QLinearGradient,
    QAction, QKeySequence, QShortcut, QPalette,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QStackedLayout, QLabel, QPushButton, QFrame,
    QComboBox, QLineEdit, QCheckBox, QProgressBar, QScrollArea,
    QFileDialog, QMessageBox, QDialog, QDialogButtonBox,
    QGroupBox, QButtonGroup, QRadioButton, QSpinBox,
    QSizePolicy, QSpacerItem, QFrame, QStatusBar,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView,
    QTabWidget, QTextEdit, QSplitter, QToolBar, QToolButton,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect,
)

# ────────── 设计系统常量 ──────────
# 配色方案 (Soft UI Evolution)
COLORS = {
    "primary": "#0D9488",
    "primary_dark": "#0F766E",
    "primary_light": "#14B8A6",
    "primary_bg": "#F0FDFA",
    "accent": "#EA580C",
    "accent_dark": "#C2410C",
    "accent_light": "#F97316",
    "bg": "#F8FAFC",
    "surface": "#FFFFFF",
    "surface_hover": "#F1F5F9",
    "text_primary": "#1E293B",
    "text_secondary": "#64748B",
    "text_muted": "#94A3B8",
    "border": "#E2E8F0",
    "border_focus": "#0D9488",
    "success": "#10B981",
    "warning": "#F59E0B",
    "danger": "#DC2626",
}

# 间距规范 (8px 基准网格)
SPACING = {
    "xs": 4,
    "sm": 8,
    "md": 12,
    "lg": 16,
    "xl": 24,
    "xxl": 32,
}

# 圆角
RADIUS = {
    "sm": 6,
    "md": 8,
    "lg": 12,
    "xl": 16,
    "pill": 999,
}

# 动画时长
ANIM = {
    "fast": 150,
    "normal": 200,
    "slow": 300,
}

_FONT_FAMILY = "Microsoft YaHei UI"
_FONT_SIZE = 10


def get_resource_path(relative_path):
    """获取资源路径（PyInstaller 兼容）"""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)


def load_stylesheet():
    """加载 QSS 样式表"""
    qss_path = get_resource_path(os.path.join("styles", "app.qss"))
    if os.path.exists(qss_path):
        with open(qss_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def load_embedded_font():
    """加载嵌入式思源黑体作为备选字体"""
    font_path = get_resource_path(os.path.join("fonts", "NotoSansSC-Regular.ttf"))
    if os.path.exists(font_path):
        QFontDatabase.addApplicationFont(font_path)


def pil_to_qpixmap(img):
    """Pillow Image 转 QPixmap"""
    if img.mode == "RGB":
        qimg = QImage(img.data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
    elif img.mode == "RGBA":
        qimg = QImage(img.data, img.width, img.height, img.width * 4, QImage.Format_RGBA8888)
    else:
        img = img.convert("RGBA")
        qimg = QImage(img.data, img.width, img.height, img.width * 4, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


def add_shadow(widget, blur=20, offset=4, color=QColor(0, 0, 0, 30)):
    """添加阴影效果"""
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(blur)
    shadow.setOffset(offset, offset)
    shadow.setColor(color)
    widget.setGraphicsEffect(shadow)


def add_fade_animation(widget, duration=ANIM["normal"]):
    """添加淡入动画"""
    effect = QGraphicsOpacityEffect()
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity")
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.start()
    return anim


# ────────── 自定义 Widget ──────────
class ScannerCard(QFrame):
    """扫描仪卡片组件（带动画效果）"""
    clicked = Signal(int)

    def __init__(self, idx: int, scanner: ScannerInfo, parent=None):
        super().__init__(parent)
        self.idx = idx
        self.scanner = scanner
        self._selected = False
        self._hovered = False

        self.setFixedHeight(76)
        self.setCursor(Qt.PointingHandCursor)
        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("scannerCard")

        # 阴影
        add_shadow(self, blur=16, offset=2, color=QColor(0, 0, 0, 20))

        self._setup_ui()
        self._update_style()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

        # 状态指示器
        self.status_dot = QLabel("●")
        self.status_dot.setFixedWidth(16)
        self.status_dot.setStyleSheet(f"color: {COLORS['success']}; font-size: 14px;")
        layout.addWidget(self.status_dot)

        # 名称和信息
        info_layout = QVBoxLayout()
        info_layout.setSpacing(3)

        name = _label_for(self.scanner)
        self.name_label = QLabel(name)
        self.name_label.setFont(QFont(_FONT_FAMILY, 11, QFont.Bold))
        self.name_label.setStyleSheet(f"color: {COLORS['text_primary']};")
        info_layout.addWidget(self.name_label)

        ip_text = _ip_label_for(self.scanner)
        self.ip_label = QLabel(ip_text)
        self.ip_label.setFont(QFont(_FONT_FAMILY, 9))
        self.ip_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        info_layout.addWidget(self.ip_label)

        layout.addLayout(info_layout, 1)

        # 功能标签
        tags = []
        if self.scanner.has_adf:
            tags.append("ADF")
        if self.scanner.has_duplex:
            tags.append("双面")
        if self.scanner.escl_url:
            tags.append("eSCL")
        if tags:
            tags_text = " · ".join(tags)
            tags_label = QLabel(tags_text)
            tags_label.setFont(QFont(_FONT_FAMILY, 8))
            tags_label.setStyleSheet(
                f"color: {COLORS['primary']}; background: {COLORS['primary_bg']};"
                f"padding: 3px 8px; border-radius: {RADIUS['sm']}px;"
            )
            layout.addWidget(tags_label)

    def _update_style(self):
        border_color = COLORS["primary"] if self._selected else COLORS["border"]
        bg = COLORS["primary_bg"] if self._selected else COLORS["surface"]
        self.setStyleSheet(f"""
            ScannerCard {{
                background: {bg};
                border: 2px solid {border_color};
                border-radius: {RADIUS['lg']}px;
            }}
        """)

    def set_selected(self, selected: bool):
        self._selected = selected
        self._update_style()
        # 选中时增加阴影
        if selected:
            add_shadow(self, blur=24, offset=4, color=QColor(13, 148, 136, 40))
        else:
            add_shadow(self, blur=16, offset=2, color=QColor(0, 0, 0, 20))

    def mousePressEvent(self, event):
        self.clicked.emit(self.idx)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        self._hovered = True
        if not self._selected:
            self.setStyleSheet(f"""
                ScannerCard {{
                    background: {COLORS['surface_hover']};
                    border: 2px solid {COLORS['primary_light']};
                    border-radius: {RADIUS['lg']}px;
                }}
            """)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._update_style()
        super().leaveEvent(event)


class ImageViewer(QWidget):
    """图像预览组件（支持缩放和旋转）"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._scaled_pixmap = None
        self._rotation = 0
        self.setMinimumSize(300, 200)
        self.setObjectName("imageViewer")

    def set_image(self, pixmap: QPixmap, rotation=0):
        self._pixmap = pixmap
        self._rotation = rotation
        self._update_scaled()
        self.update()

    def _update_scaled(self):
        if self._pixmap is None:
            return
        transform =()
        if self._rotation:
            transform = transform.rotate(self._rotation)
        pm = self._pixmap.transformed(transform)
        self._scaled_pixmap = pm.scaled(
            self.size() - QSize(20, 20),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if self._scaled_pixmap:
            x = (self.width() - self._scaled_pixmap.width()) // 2
            y = (self.height() - self._scaled_pixmap.height()) // 2
            painter.drawPixmap(x, y, self._scaled_pixmap)
        else:
            painter.setPen(QColor(COLORS["text_muted"]))
            painter.drawText(self.rect(), Qt.AlignCenter, "无预览")
        painter.end()

    def resizeEvent(self, event):
        self._update_scaled()
        super().resizeEvent(event)

    def rotate_left(self):
        self._rotation = (self._rotation - 90) % 360
        self._update_scaled()
        self.update()

    def rotate_right(self):
        self._rotation = (self._rotation + 90) % 360
        self._update_scaled()
        self.update()

    def get_rotation(self):
        return self._rotation


class AnimatedButton(QPushButton):
    """带动画效果的按钮"""
    def __init__(self, text, parent=None, button_type="primary"):
        super().__init__(text, parent)
        self.button_type = button_type
        self.setCursor(Qt.PointingHandCursor)
        self._update_style()

    def _update_style(self):
        if self.button_type == "primary":
            self.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                                stop:0 {COLORS['primary']}, stop:1 {COLORS['primary_light']});
                    color: white;
                    border: none;
                    padding: 10px 24px;
                    border-radius: {RADIUS['md']}px;
                    font-size: 14px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                                stop:0 {COLORS['primary_dark']}, stop:1 {COLORS['primary']});
                }}
                QPushButton:pressed {{
                    background: {COLORS['primary_dark']};
                }}
                QPushButton:disabled {{
                    background: {COLORS['text_muted']};
                    color: {COLORS['border']};
                }}
            """)
        elif self.button_type == "secondary":
            self.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['surface']};
                    color: {COLORS['text_primary']};
                    border: 1.5px solid {COLORS['border']};
                    padding: 8px 16px;
                    border-radius: {RADIUS['md']}px;
                    font-weight: 500;
                }}
                QPushButton:hover {{
                    border-color: {COLORS['primary']};
                    color: {COLORS['primary']};
                    background: {COLORS['primary_bg']};
                }}
                QPushButton:pressed {{
                    background: {COLORS['surface_hover']};
                }}
            """)
        elif self.button_type == "accent":
            self.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                                stop:0 {COLORS['accent']}, stop:1 {COLORS['accent_light']});
                    color: white;
                    border: none;
                    padding: 8px 16px;
                    border-radius: {RADIUS['md']}px;
                    font-weight: 500;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                                stop:0 {COLORS['accent_dark']}, stop:1 {COLORS['accent']});
                }}
            """)

    def mousePressEvent(self, event):
        # 点击缩放动画
        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(ANIM["fast"])
        self._anim.setStartValue(self.geometry())
        rect = self.geometry()
        self._anim.setEndValue(QRect(rect.x() + 1, rect.y() + 1, rect.width() - 2, rect.height() - 2))
        self._anim.setEasingCurve(QEasingCurve.OutQuad)
        self._anim.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(ANIM["fast"])
        self._anim.setStartValue(self.geometry())
        rect = self.geometry()
        self._anim.setEndValue(QRect(rect.x() - 1, rect.y() - 1, rect.width() + 2, rect.height() + 2))
        self._anim.setEasingCurve(QEasingCurve.OutQuad)
        self._anim.start()
        super().mouseReleaseEvent(event)


# ────────── 主窗口 ──────────
class ScanApp(QMainWindow):
    """主窗口（UI 优化版）"""

    def __init__(self):
        super().__init__()

        # 业务逻辑层
        self.coordinator = ScanCoordinator()
        self.coordinator.load_config()
        self.cfg = self.coordinator.cfg

        # 启动时清理过期缓存
        try:
            self.coordinator.cleanup_stale_cache()
        except Exception:
            pass

        # 状态
        self.selected: Optional[ScannerInfo] = None
        self.selected_idx: int = -1
        self.output_dir = self.cfg.get("output_dir", DEFAULT_OUT_DIR)
        self.resolution_val = self.cfg.get("resolution", 300)
        self.color_mode_val = self.cfg.get("color_mode_ui", "彩色")
        self.output_format_val = self.cfg.get("output_format", "jpg")
        self.source_val = self.cfg.get("source", "平板")
        self.duplex_val = False
        self.extra_dirs = []
        self.cards = []

        self._setup_ui()
        self._auto_discover()

    def _setup_ui(self):
        self.setWindowTitle("HP Scan Tool v4.0")
        self.setMinimumSize(900, 620)
        self.resize(1050, 720)

        # 加载字体
        load_embedded_font()

        # 设置窗口图标
        icon_path = get_resource_path("icon_64.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # 中央部件
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 左侧：扫描仪列表
        self._build_scanner_panel(main_layout)

        # 右侧：参数面板
        self._build_param_panel(main_layout)

        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")

        # 恢复已保存的扫描仪
        saved = self.coordinator.restore_saved_scanners()
        if saved:
            self._rebuild_cards()
            self.status_bar.showMessage(f"已恢复 {len(saved)} 台扫描仪")

        # 进度条（嵌入状态栏）
        self.scan_progress = QProgressBar()
        self.scan_progress.setMaximumWidth(200)
        self.scan_progress.setMaximumHeight(16)
        self.scan_progress.setVisible(False)
        self.status_bar.addPermanentWidget(self.scan_progress)

        # 淡入动画
        add_fade_animation(self, ANIM["slow"])

    def _build_scanner_panel(self, parent_layout):
        """构建左侧扫描仪列表面板"""
        panel = QFrame()
        panel.setFixedWidth(280)
        panel.setObjectName("scannerPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(SPACING["md"], SPACING["md"], SPACING["md"], SPACING["md"])
        layout.setSpacing(SPACING["sm"])

        # 标题
        title = QLabel("扫描仪")
        title.setFont(QFont(_FONT_FAMILY, 13, QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['text_primary']};")
        layout.addWidget(title)

        # 刷新按钮
        refresh_btn = AnimatedButton("刷新", button_type="secondary")
        refresh_btn.clicked.connect(self._auto_discover)
        layout.addWidget(refresh_btn)

        # 扫描仪卡片滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("border: none; background: transparent;")

        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(SPACING["sm"])
        self.cards_layout.addStretch(1)
        scroll.setWidget(self.cards_container)
        layout.addWidget(scroll)

        parent_layout.addWidget(panel)

    def _build_param_panel(self, parent_layout):
        """构建右侧参数面板"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(SPACING["lg"], SPACING["lg"], SPACING["lg"], SPACING["lg"])
        layout.setSpacing(SPACING["md"])

        # 参数组
        params_group = QGroupBox("扫描参数")
        params_group.setFont(QFont(_FONT_FAMILY, 10, QFont.Bold))
        params_layout = QGridLayout(params_group)
        params_layout.setSpacing(SPACING["md"])

        # 分辨率
        params_layout.addWidget(QLabel("分辨率:"), 0, 0)
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(["150", "200", "300", "600", "1200"])
        self.resolution_combo.setCurrentText(str(self.resolution_val))
        self.resolution_combo.currentTextChanged.connect(self._on_resolution_changed)
        params_layout.addWidget(self.resolution_combo, 0, 1)

        # 颜色模式
        params_layout.addWidget(QLabel("颜色:"), 1, 0)
        self.color_mode_combo = QComboBox()
        self.color_mode_combo.addItems(["彩色", "灰度", "黑白"])
        self.color_mode_combo.setCurrentText(self.color_mode_val)
        self.color_mode_combo.currentTextChanged.connect(self._on_color_mode_changed)
        params_layout.addWidget(self.color_mode_combo, 1, 1)

        # 输出格式
        params_layout.addWidget(QLabel("格式:"), 2, 0)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["jpg", "png", "pdf", "tiff"])
        self.format_combo.setCurrentText(self.output_format_val)
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        params_layout.addWidget(self.format_combo, 2, 1)

        # 来源
        params_layout.addWidget(QLabel("来源:"), 3, 0)
        self.source_combo = QComboBox()
        self.source_combo.addItems(["平板", "ADF"])
        self.source_combo.setCurrentText(self.source_val)
        self.source_combo.currentTextChanged.connect(self._on_source_changed)
        params_layout.addWidget(self.source_combo, 3, 1)

        # 双面扫描
        self.duplex_check = QCheckBox("双面扫描")
        self.duplex_check.stateChanged.connect(self._on_duplex_changed)
        params_layout.addWidget(self.duplex_check, 4, 0, 1, 2)

        layout.addWidget(params_group)

        # 保存目录
        dir_group = QGroupBox("保存位置")
        dir_group.setFont(QFont(_FONT_FAMILY, 10, QFont.Bold))
        dir_layout = QHBoxLayout(dir_group)
        self.dir_entry = QLineEdit(self.output_dir)
        self.dir_entry.setReadOnly(True)
        dir_layout.addWidget(self.dir_entry)
        browse_btn = AnimatedButton("浏览", button_type="secondary")
        browse_btn.clicked.connect(self._browse)
        dir_layout.addWidget(browse_btn)
        layout.addWidget(dir_group)

        # 额外输出目标
        extra_group = QGroupBox("额外输出（多目标）")
        extra_group.setFont(QFont(_FONT_FAMILY, 10, QFont.Bold))
        extra_layout = QVBoxLayout(extra_group)
        self.extra_list_widget = QLabel("无额外输出目录")
        self.extra_list_widget.setStyleSheet(f"color: {COLORS['text_muted']};")
        extra_layout.addWidget(self.extra_list_widget)
        extra_btn_layout = QHBoxLayout()
        add_extra_btn = AnimatedButton("添加", button_type="secondary")
        add_extra_btn.clicked.connect(self._add_extra_dir)
        extra_btn_layout.addWidget(add_extra_btn)
        clear_extra_btn = AnimatedButton("清空", button_type="secondary")
        clear_extra_btn.clicked.connect(self._clear_extra_dirs)
        extra_btn_layout.addWidget(clear_extra_btn)
        extra_layout.addLayout(extra_btn_layout)
        layout.addWidget(extra_group)

        # 操作按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(SPACING["sm"])

        self.scan_btn = AnimatedButton("扫描", button_type="primary")
        self.scan_btn.clicked.connect(self._start_scan)
        btn_layout.addWidget(self.scan_btn)

        listen_btn = AnimatedButton("监听模式", button_type="accent")
        listen_btn.clicked.connect(self._start_listen)
        btn_layout.addWidget(listen_btn)

        history_btn = AnimatedButton("扫描历史", button_type="secondary")
        history_btn.clicked.connect(self._show_history)
        btn_layout.addWidget(history_btn)

        layout.addLayout(btn_layout)
        layout.addStretch(1)

        parent_layout.addWidget(panel)

    # ────────── 事件处理 ──────────
    def _on_resolution_changed(self, text):
        self.resolution_val = int(text)
        self.cfg["resolution"] = self.resolution_val
        save_config(self.cfg)

    def _on_color_mode_changed(self, text):
        self.color_mode_val = text
        self.cfg["color_mode_ui"] = self.color_mode_val
        save_config(self.cfg)

    def _on_format_changed(self, text):
        self.output_format_val = text
        self.cfg["output_format"] = self.output_format_val
        save_config(self.cfg)

    def _on_source_changed(self, text):
        self.source_val = text
        self.cfg["source"] = self.source_val
        save_config(self.cfg)
        self._update_duplex_visibility()

    def _on_duplex_changed(self, state):
        self.duplex_val = bool(state)

    def _update_duplex_visibility(self):
        """根据来源更新双面复选框可见性"""
        has_adf = self.source_val == "ADF"
        has_duplex = self.selected and self.selected.has_duplex
        self.duplex_check.setVisible(has_adf and has_duplex)
        if not has_adf:
            self.duplex_check.setChecked(False)

    def _browse(self):
        p = QFileDialog.getExistingDirectory(self, "选择保存目录", self.output_dir)
        if p:
            self.output_dir = p
            self.dir_entry.setText(p)
            self.cfg["output_dir"] = p
            save_config(self.cfg)

    def _add_extra_dir(self):
        p = QFileDialog.getExistingDirectory(self, "选择额外输出目录")
        if p and p not in self.extra_dirs:
            self.extra_dirs.append(p)
            self._refresh_extra_dirs()

    def _clear_extra_dirs(self):
        self.extra_dirs.clear()
        self._refresh_extra_dirs()

    def _refresh_extra_dirs(self):
        if self.extra_dirs:
            text = "\n".join(self.extra_dirs)
            self.extra_list_widget.setText(text)
            self.extra_list_widget.setStyleSheet(f"color: {COLORS['text_primary']};")
        else:
            self.extra_list_widget.setText("无额外输出目录")
            self.extra_list_widget.setStyleSheet(f"color: {COLORS['text_muted']};")

    # ────────── 扫描仪管理 ──────────
    def _auto_discover(self):
        """自动发现扫描仪"""
        self.status_bar.showMessage("正在发现扫描仪...")

        def _on_complete(new_count, new_scanners):
            QTimer.singleShot(0, self._on_discover_finished)

        self.coordinator.discover_scanners_background(_on_complete)

    def _on_discover_finished(self):
        """发现完成更新 UI"""
        self._rebuild_cards()
        count = len(self.coordinator.scanners)
        self.status_bar.showMessage(f"发现 {count} 台扫描仪")

    def _rebuild_cards(self):
        """重建扫描仪卡片列表"""
        for card in self.cards:
            card.deleteLater()
        self.cards.clear()

        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.spacerItem():
                self.cards_layout.removeItem(item)

        for idx, scanner in enumerate(self.coordinator.scanners):
            card = ScannerCard(idx, scanner)
            card.clicked.connect(self._on_card_clicked)
            self.cards_layout.addWidget(card)
            self.cards.append(card)

        self.cards_layout.addStretch(1)

    def _on_card_clicked(self, idx: int):
        """点击扫描仪卡片"""
        if 0 <= idx < len(self.coordinator.scanners):
            self.selected = self.coordinator.scanners[idx]
            self.selected_idx = idx

            for i, card in enumerate(self.cards):
                card.set_selected(i == idx)

            self._update_source_options(self.selected.has_adf, self.selected.has_duplex)
            self._load_profile()

            label = _label_for(self.selected)
            self.status_bar.showMessage(f"已选择: {label}")

            if self.selected.ip:
                self.cfg["selected_scanner_ip"] = self.selected.ip
                save_config(self.cfg)

    def _update_source_options(self, has_adf: bool, has_duplex: bool):
        """更新来源选项"""
        self.source_combo.clear()
        self.source_combo.addItem("平板")
        if has_adf:
            self.source_combo.addItem("ADF")
        self._update_duplex_visibility()

    # ────────── 配置文件 ──────────
    def _load_profile(self):
        """加载当前设备的配置文件"""
        if not self.selected or not self.selected.ip:
            return
        profile = profile_manager.get_profile(self.selected.ip)
        if not profile:
            return

        if "resolution" in profile:
            self.resolution_val = profile["resolution"]
            self.resolution_combo.setCurrentText(str(self.resolution_val))
        if "color_mode" in profile:
            self.color_mode_val = profile["color_mode"]
            self.color_mode_combo.setCurrentText(self.color_mode_val)
        if "output_format" in profile:
            self.output_format_val = profile["output_format"]
            self.format_combo.setCurrentText(self.output_format_val)
        if "source" in profile:
            self.source_val = profile["source"]
            self.source_combo.setCurrentText(self.source_val)

        self.status_bar.showMessage(f"已加载 {self.selected.display_name} 的配置文件")

    def _save_profile(self):
        """保存当前设备的配置文件"""
        if not self.selected or not self.selected.ip:
            return
        profile = {
            "resolution": self.resolution_val,
            "color_mode": self.color_mode_val,
            "output_format": self.output_format_val,
            "source": self.source_combo.currentText(),
        }
        profile_manager.save_profile(self.selected.ip, profile)

    # ────────── 扫描 ──────────
    def _start_scan(self):
        """开始扫描"""
        if not self.selected:
            QMessageBox.warning(self, "提示", "请先选择一台打印机")
            return

        out_dir = self.dir_entry.text() or self.output_dir
        try:
            out_dir = os.path.abspath(out_dir)
        except (ValueError, OSError):
            QMessageBox.critical(self, "错误", "保存路径无效")
            return
        if not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        self._save_profile()

        scanner = self.selected
        source_val = "Platen" if "平板" in self.source_combo.currentText() else "Feeder"
        use_adf = source_val == "Feeder"

        self.scan_btn.setText("扫描中...")
        self.scan_btn.setEnabled(False)
        self.status_bar.showMessage("正在扫描...")
        self.scan_progress.setVisible(True)
        self.scan_progress.setRange(0, 0)

        def _scan_thread():
            try:
                if use_adf:
                    self._do_multipage_scan(scanner, out_dir, source_val)
                else:
                    self._do_single_scan(scanner, out_dir, source_val)
            except Exception as e:
                QTimer.singleShot(0, lambda: self._scan_error(f"{type(e).__name__}: {e}"))
            finally:
                QTimer.singleShot(0, self._reset_scan_ui)

        threading.Thread(target=_scan_thread, daemon=True).start()

    def _do_single_scan(self, scanner, out_dir, source_val):
        """单页扫描"""
        from escl_engine import execute_scan

        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        cache_manager.register_job(job_id)

        data, ext = execute_scan(
            scanner=scanner,
            resolution=self.resolution_val,
            color_mode=COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
            output_format=self.output_format_val,
            source=source_val,
            duplex=self.duplex_val,
        )

        cache_path = cache_manager.write_page(job_id, 1, data, ext)
        del data

        if is_blank_page(Image.open(cache_path)):
            QTimer.singleShot(0, lambda: self.status_bar.showMessage("检测到空白页"))

        output_path = os.path.join(out_dir, f"HP_Scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")
        QTimer.singleShot(0, lambda: self._show_preview(cache_path, ext, output_path, job_id, scanner))

    def _do_multipage_scan(self, scanner, out_dir, source_val):
        """多页扫描"""
        from escl_engine import execute_multipage_scan

        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        cache_manager.register_job(job_id)

        pages = execute_multipage_scan(
            scanner=scanner,
            resolution=self.resolution_val,
            color_mode=COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
            output_format=self.output_format_val,
            source=source_val,
            duplex=self.duplex_val,
        )

        ext = pages[0][1] if pages else self.output_format_val
        for i, (data, ext) in enumerate(pages, 1):
            cache_manager.write_page(job_id, i, data, ext)
            del data

        if len(pages) > 1:
            self._detect_blank_pages(job_id, len(pages), ext)

        output_path = os.path.join(out_dir, f"HP_Scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")
        QTimer.singleShot(0, lambda: self._show_multi_preview(
            job_id, ext, output_path, len(pages), scanner))

    def _detect_blank_pages(self, job_id, page_count, ext):
        """检测空白页"""
        blank_pages = []
        for i in range(1, page_count + 1):
            try:
                cache_path = os.path.join(
                    cache_manager._job_dir(job_id), f"page_{i:03d}.{ext}")
                if os.path.exists(cache_path):
                    img = Image.open(cache_path)
                    if is_blank_page(img):
                        blank_pages.append(i)
            except Exception:
                pass

        if blank_pages:
            page_str = ", ".join(str(p) for p in blank_pages[:10])
            QTimer.singleShot(0, lambda: self.status_bar.showMessage(
                f"检测到 {len(blank_pages)} 页空白页: {page_str}"))

    def _reset_scan_ui(self):
        """重置扫描 UI"""
        self.scan_btn.setText("扫描")
        self.scan_btn.setEnabled(True)
        self.scan_progress.setVisible(False)

    def _scan_error(self, msg):
        """扫描错误"""
        self._reset_scan_ui()
        QMessageBox.critical(self, "扫描错误", msg)
        self.status_bar.showMessage(f"扫描失败: {msg[:50]}")

    # ────────── 预览对话框 ──────────
    def _show_preview(self, cache_path, ext, output_path, job_id, scanner=None):
        """显示单页预览"""
        self._reset_scan_ui()
        dev_name = (scanner.model or "") if scanner else ""
        dev_ip = scanner.ip if scanner else ""
        dialog = PreviewDialog(self, cache_path, ext, output_path, job_id,
                               device_name=dev_name, device_ip=dev_ip,
                               extra_dirs=self.extra_dirs)
        dialog.exec()

    def _show_multi_preview(self, job_id, ext, output_path, page_count, scanner=None):
        """显示多页预览"""
        self._reset_scan_ui()
        dev_name = (scanner.model or "") if scanner else ""
        dev_ip = scanner.ip if scanner else ""
        dialog = MultiPagePreviewDialog(self, job_id, ext, output_path, page_count,
                                        device_name=dev_name, device_ip=dev_ip,
                                        extra_dirs=self.extra_dirs)
        dialog.exec()

    # ────────── 监听模式 ──────────
    def _start_listen(self):
        """启动监听模式"""
        if not self.selected:
            QMessageBox.warning(self, "提示", "请先选择一台打印机")
            return

        from listen_engine import listen_for_scan, listen_for_multipage_scan

        out_dir = self.dir_entry.text() or self.output_dir
        try:
            out_dir = os.path.abspath(out_dir)
        except (ValueError, OSError):
            QMessageBox.critical(self, "错误", "保存路径无效")
            return
        if not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        scanner = self.selected
        source_val = "Platen" if "平板" in self.source_combo.currentText() else "Feeder"
        use_adf = source_val == "Feeder"

        self.scan_btn.setText("监听中...")
        self.scan_btn.setEnabled(False)
        self.status_bar.showMessage("监听模式：等待打印机面板触发扫描...")

        def _progress(msg, page=0):
            QTimer.singleShot(0, lambda: self.status_bar.showMessage(f"监听模式：{msg}"))

        def _listen_thread():
            try:
                if use_adf:
                    pages = listen_for_multipage_scan(
                        scanner=scanner,
                        resolution=self.resolution_val,
                        color_mode=COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
                        output_format=self.output_format_val,
                        source=source_val,
                        duplex=self.duplex_val,
                        wait_timeout=600.0,
                        progress_callback=_progress,
                    )
                    if pages:
                        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        cache_manager.register_job(job_id)
                        ext = pages[0][1] if pages else self.output_format_val
                        for i, (data, ext) in enumerate(pages, 1):
                            cache_manager.write_page(job_id, i, data, ext)
                            del data
                        output_path = os.path.join(out_dir, f"HP_Listen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")
                        QTimer.singleShot(0, lambda: self._show_multi_preview(
                            job_id, ext, output_path, len(pages), scanner))
                    else:
                        QTimer.singleShot(0, lambda: self.status_bar.showMessage("监听模式：超时或已取消"))
                else:
                    result = listen_for_scan(
                        scanner=scanner,
                        resolution=self.resolution_val,
                        color_mode=COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
                        output_format=self.output_format_val,
                        source=source_val,
                        duplex=self.duplex_val,
                        wait_timeout=300.0,
                        progress_callback=_progress,
                    )
                    if result:
                        data, ext = result
                        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        cache_manager.register_job(job_id)
                        cache_path = cache_manager.write_page(job_id, 1, data, ext)
                        del data
                        output_path = os.path.join(out_dir, f"HP_Listen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")
                        QTimer.singleShot(0, lambda: self._show_preview(
                            cache_path, ext, output_path, job_id, scanner))
                    else:
                        QTimer.singleShot(0, lambda: self.status_bar.showMessage("监听模式：超时或已取消"))
            except Exception as e:
                QTimer.singleShot(0, lambda: self._scan_error(f"监听模式错误: {e}"))
            finally:
                QTimer.singleShot(0, self._reset_scan_ui)

        threading.Thread(target=_listen_thread, daemon=True).start()

    # ────────── 历史记录 ──────────
    def _show_history(self):
        """显示扫描历史"""
        dialog = HistoryDialog(self)
        dialog.exec()


# ────────── 预览对话框 ──────────
class PreviewDialog(QDialog):
    """单页预览对话框（UI 优化版）"""

    def __init__(self, parent, cache_path, ext, output_path, job_id,
                 device_name="", device_ip="", extra_dirs=None):
        super().__init__(parent)
        self.cache_path = cache_path
        self.ext = ext
        self.output_path = output_path
        self.job_id = job_id
        self.device_name = device_name
        self.device_ip = device_ip
        self.extra_dirs = extra_dirs or []
        self._rotation = 0
        self._cropped = False

        self.setWindowTitle("扫描预览")
        self.setMinimumSize(520, 420)
        self.resize(620, 520)

        self._setup_ui()
        self._show_image()
        add_fade_animation(self, ANIM["normal"])

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING["md"])

        # 图像预览
        self.image_viewer = ImageViewer()
        layout.addWidget(self.image_viewer, 1)

        # 信息标签
        self.info_label = QLabel()
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(self.info_label)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(SPACING["sm"])

        rotate_left_btn = AnimatedButton("↺ 左转", button_type="secondary")
        rotate_left_btn.clicked.connect(self._rotate_left)
        btn_layout.addWidget(rotate_left_btn)

        rotate_right_btn = AnimatedButton("↻ 右转", button_type="secondary")
        rotate_right_btn.clicked.connect(self._rotate_right)
        btn_layout.addWidget(rotate_right_btn)

        crop_btn = AnimatedButton("自动裁边", button_type="secondary")
        crop_btn.clicked.connect(self._auto_crop)
        btn_layout.addWidget(crop_btn)

        btn_layout.addStretch()

        cancel_btn = AnimatedButton("重新扫描", button_type="secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = AnimatedButton("保存", button_type="primary")
        save_btn.clicked.connect(self._confirm)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _show_image(self):
        try:
            img = Image.open(self.cache_path)
            if self._rotation:
                img = img.rotate(self._rotation, expand=True)
            pixmap = pil_to_qpixmap(img)
            self.image_viewer.set_image(pixmap, self._rotation)
            w, h = img.size
            rot_text = f" (旋转 {self._rotation}°)" if self._rotation else ""
            self.info_label.setText(f"{w} × {h}{rot_text}")
        except Exception as e:
            self.info_label.setText(f"预览失败: {e}")

    def _rotate_left(self):
        self._rotation = (self._rotation - 90) % 360
        self._show_image()

    def _rotate_right(self):
        self._rotation = (self._rotation + 90) % 360
        self._show_image()

    def _auto_crop(self):
        try:
            img = Image.open(self.cache_path)
            cropped = auto_crop(img)
            if cropped is not img:
                crop_path = self.cache_path + ".cropped"
                cropped.save(crop_path, quality=95)
                self.cache_path = crop_path
                self._cropped = True
                self._rotation = 0
                self._show_image()
        except Exception as e:
            self.info_label.setText(f"裁边失败: {e}")

    def _confirm(self):
        """保存"""
        save_path = self.output_path
        out_ext = self.ext

        try:
            img = Image.open(self.cache_path)
            if self._rotation:
                img = img.rotate(self._rotation, expand=True)

            if out_ext == "pdf":
                pdf_path = save_path if save_path.lower().endswith(".pdf") \
                           else save_path.rsplit(".", 1)[0] + ".pdf"
                img.convert("RGB").save(pdf_path, "PDF", resolution=150.0)
                saved = pdf_path
            else:
                final = save_path
                if not final.lower().endswith(f".{out_ext}"):
                    final = f"{final.rsplit('.', 1)[0]}.{out_ext}"
                if out_ext in ("jpg", "jpeg"):
                    img.convert("RGB").save(final, "JPEG", quality=95)
                elif out_ext == "png":
                    img.save(final, "PNG")
                elif out_ext in ("tiff", "tif"):
                    img.save(final, "TIFF")
                saved = final

            for extra_dir in self.extra_dirs:
                try:
                    os.makedirs(extra_dir, exist_ok=True)
                    extra_path = os.path.join(extra_dir, os.path.basename(saved))
                    if extra_path != saved:
                        shutil.copy2(saved, extra_path)
                except Exception:
                    pass

            cache_manager.remove_job(self.job_id)

            history_manager.append({
                "device_name": self.device_name,
                "device_ip": self.device_ip,
                "page_count": 1,
                "format": out_ext,
                "file_path": saved,
            })

            self.accept()
            try:
                os.startfile(os.path.dirname(saved))
            except Exception:
                pass

        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))


class MultiPagePreviewDialog(QDialog):
    """多页预览对话框（UI 优化版）"""

    TW, TH = 80, 100

    def __init__(self, parent, job_id, ext, output_path, page_count,
                 device_name="", device_ip="", extra_dirs=None):
        super().__init__(parent)
        self.job_id = job_id
        self.ext = ext
        self.output_path = output_path
        self.device_name = device_name
        self.device_ip = device_ip
        self.extra_dirs = extra_dirs or []

        self.pages = list(range(1, page_count + 1))
        self.selected_idx = 0
        self.splits = set()
        self.group_names = {}
        self._rotations = {}
        self._cropped = set()

        self.setWindowTitle("多页预览")
        self.setMinimumSize(720, 520)
        self.resize(820, 620)

        self._setup_ui()
        self._update_preview()
        self._refresh_thumbs()
        add_fade_animation(self, ANIM["normal"])

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING["md"])

        splitter = QSplitter(Qt.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.thumbs_scroll = QScrollArea()
        self.thumbs_scroll.setWidgetResizable(True)
        self.thumbs_scroll.setFixedWidth(120)
        self.thumbs_container = QWidget()
        self.thumbs_layout = QVBoxLayout(self.thumbs_container)
        self.thumbs_layout.setSpacing(4)
        self.thumbs_layout.addStretch(1)
        self.thumbs_scroll.setWidget(self.thumbs_container)
        left_layout.addWidget(self.thumbs_scroll)

        splitter.addWidget(left_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.image_viewer = ImageViewer()
        right_layout.addWidget(self.image_viewer, 1)

        self.page_info_label = QLabel()
        self.page_info_label.setAlignment(Qt.AlignCenter)
        self.page_info_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        right_layout.addWidget(self.page_info_label)

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(SPACING["sm"])

        rotate_left_btn = AnimatedButton("↺ 左转", button_type="secondary")
        rotate_left_btn.clicked.connect(self._rotate_left)
        btn_layout.addWidget(rotate_left_btn)

        rotate_right_btn = AnimatedButton("↻ 右转", button_type="secondary")
        rotate_right_btn.clicked.connect(self._rotate_right)
        btn_layout.addWidget(rotate_right_btn)

        crop_btn = AnimatedButton("裁边", button_type="secondary")
        crop_btn.clicked.connect(self._auto_crop)
        btn_layout.addWidget(crop_btn)

        crop_all_btn = AnimatedButton("全部裁边", button_type="secondary")
        crop_all_btn.clicked.connect(self._auto_crop_all)
        btn_layout.addWidget(crop_all_btn)

        btn_layout.addStretch()

        cancel_btn = AnimatedButton("重新扫描", button_type="secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = AnimatedButton("保存全部", button_type="primary")
        save_btn.clicked.connect(self._confirm)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _cache_path(self, page_num):
        return os.path.join(cache_manager._job_dir(self.job_id), f"page_{page_num:03d}.{self.ext}")

    def _get_rotation(self, page_num):
        return self._rotations.get(page_num, 0)

    def _update_preview(self):
        try:
            page_num = self.pages[self.selected_idx]
            img = Image.open(self._cache_path(page_num))
            rotation = self._get_rotation(page_num)
            if rotation:
                img = img.rotate(rotation, expand=True)
            pixmap = pil_to_qpixmap(img)
            self.image_viewer.set_image(pixmap, rotation)
            w, h = img.size
            rot_text = f" (旋转 {rotation}°)" if rotation else ""
            self.page_info_label.setText(
                f"第 {self.selected_idx + 1} 页 / 共 {len(self.pages)} 页{rot_text}")
        except Exception as e:
            self.page_info_label.setText(f"预览失败: {e}")

    def _refresh_thumbs(self):
        for i in reversed(range(self.thumbs_layout.count())):
            widget = self.thumbs_layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()

        for idx, page_num in enumerate(self.pages):
            try:
                img = Image.open(self._cache_path(page_num))
                rotation = self._get_rotation(page_num)
                if rotation:
                    img = img.rotate(rotation, expand=True)
                img.thumbnail((self.TW, self.TH), Image.LANCZOS)
                pixmap = pil_to_qpixmap(img)

                thumb_btn = QPushButton()
                thumb_btn.setIcon(QIcon(pixmap))
                thumb_btn.setIconSize(QSize(self.TW, self.TH))
                thumb_btn.setFixedSize(self.TW + 8, self.TH + 8)
                thumb_btn.setCursor(Qt.PointingHandCursor)
                thumb_btn.clicked.connect(lambda checked=False, i=idx: self._on_thumb_clicked(i))

                if idx == self.selected_idx:
                    thumb_btn.setStyleSheet(f"border: 2px solid {COLORS['primary']}; border-radius: 6px;")
                else:
                    thumb_btn.setStyleSheet(f"border: 1px solid {COLORS['border']}; border-radius: 6px;")

                self.thumbs_layout.addWidget(thumb_btn)
            except Exception:
                pass

        self.thumbs_layout.addStretch(1)

    def _on_thumb_clicked(self, idx):
        self.selected_idx = idx
        self._update_preview()
        self._refresh_thumbs()

    def _rotate_left(self):
        page_num = self.pages[self.selected_idx]
        self._rotations[page_num] = (self._get_rotation(page_num) - 90) % 360
        self._update_preview()
        self._refresh_thumbs()

    def _rotate_right(self):
        page_num = self.pages[self.selected_idx]
        self._rotations[page_num] = (self._get_rotation(page_num) + 90) % 360
        self._update_preview()
        self._refresh_thumbs()

    def _auto_crop(self):
        page_num = self.pages[self.selected_idx]
        try:
            img = Image.open(self._cache_path(page_num))
            cropped = auto_crop(img)
            if cropped is not img:
                orig_path = self._cache_path(page_num)
                cropped.save(orig_path, quality=95)
                self._cropped.add(page_num)
                self._rotations.pop(page_num, None)
                self._update_preview()
                self._refresh_thumbs()
        except Exception as e:
            self.page_info_label.setText(f"裁边失败: {e}")

    def _auto_crop_all(self):
        for page_num in self.pages:
            try:
                img = Image.open(self._cache_path(page_num))
                cropped = auto_crop(img)
                if cropped is not img:
                    orig_path = self._cache_path(page_num)
                    cropped.save(orig_path, quality=95)
                    self._cropped.add(page_num)
                    self._rotations.pop(page_num, None)
            except Exception:
                pass
        self._update_preview()
        self._refresh_thumbs()

    def _confirm(self):
        """保存全部"""
        try:
            groups = self._build_groups()
            saved_files = []

            for group_indices in groups:
                is_pdf = self.ext == "pdf"
                base_path = self.output_path.rsplit(".", 1)[0]

                if is_pdf:
                    pdf_images = []
                    for page_idx in group_indices:
                        page_num = self.pages[page_idx]
                        try:
                            img = Image.open(self._cache_path(page_num))
                            rotation = self._get_rotation(page_num)
                            if rotation:
                                img = img.rotate(rotation, expand=True)
                            pdf_images.append(img.convert("RGB"))
                        except Exception:
                            pass
                    if pdf_images:
                        pdf_path = f"{base_path}.pdf"
                        pdf_images[0].save(pdf_path, "PDF", save_all=True,
                                            append_images=pdf_images[1:])
                        saved_files.append(pdf_path)
                else:
                    for seq, page_idx in enumerate(group_indices, 1):
                        page_num = self.pages[page_idx]
                        try:
                            cache_file = self._cache_path(page_num)
                            rotation = self._get_rotation(page_num)
                            fpath = f"{base_path}_{seq:03d}.{self.ext}"
                            if self.ext in ("jpg", "jpeg") and not rotation:
                                shutil.copy2(cache_file, fpath)
                            else:
                                img = Image.open(cache_file)
                                if rotation:
                                    img = img.rotate(rotation, expand=True)
                                if self.ext == "png":
                                    img.save(fpath, "PNG")
                                elif self.ext in ("tiff", "tif"):
                                    img.save(fpath, "TIFF")
                                else:
                                    if img.mode != "RGB":
                                        img = img.convert("RGB")
                                    img.save(fpath, "JPEG", quality=95)
                            saved_files.append(fpath)
                        except Exception:
                            pass

            for extra_dir in self.extra_dirs:
                try:
                    os.makedirs(extra_dir, exist_ok=True)
                    for fpath in saved_files:
                        extra_path = os.path.join(extra_dir, os.path.basename(fpath))
                        if extra_path != fpath:
                            shutil.copy2(fpath, extra_path)
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

            self.accept()
            try:
                os.startfile(os.path.dirname(saved_files[0]))
            except Exception:
                pass

        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    def _build_groups(self):
        if not self.splits:
            return [list(range(len(self.pages)))]
        sorted_splits = sorted(self.splits)
        groups = []
        start = 0
        for split in sorted_splits:
            if split > start:
                groups.append(list(range(start, split)))
            start = split
        if start < len(self.pages):
            groups.append(list(range(start, len(self.pages))))
        return groups


class HistoryDialog(QDialog):
    """扫描历史对话框（UI 优化版）"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("扫描历史")
        self.setMinimumSize(620, 420)
        self.resize(720, 520)

        layout = QVBoxLayout(self)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["时间", "设备", "页数", "格式", "路径"])
        self.tree.setColumnWidth(0, 150)
        self.tree.setColumnWidth(1, 120)
        self.tree.setColumnWidth(2, 50)
        self.tree.setColumnWidth(3, 50)
        self.tree.setAlternatingRowColors(True)
        layout.addWidget(self.tree)

        self._load_history()

        btn_layout = QHBoxLayout()
        clear_btn = AnimatedButton("清空历史", button_type="secondary")
        clear_btn.clicked.connect(self._clear_history)
        btn_layout.addWidget(clear_btn)
        btn_layout.addStretch()
        close_btn = AnimatedButton("关闭", button_type="primary")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        add_fade_animation(self, ANIM["normal"])

    def _load_history(self):
        history = history_manager.get_history()
        for entry in reversed(history):
            item = QTreeWidgetItem([
                entry.get("timestamp", "")[:19],
                entry.get("device_name", ""),
                str(entry.get("page_count", 1)),
                entry.get("format", ""),
                entry.get("file_path", ""),
            ])
            self.tree.addTopLevelItem(item)

    def _clear_history(self):
        reply = QMessageBox.question(self, "确认", "确定要清空所有历史记录吗？")
        if reply == QMessageBox.Yes:
            history_manager.clear()
            self.tree.clear()


# ────────── 主入口 ──────────
def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 加载样式表
    qss = load_stylesheet()
    if qss:
        app.setStyleSheet(qss)

    load_embedded_font()

    window = ScanApp()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
