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
import time
import traceback
from datetime import datetime
from typing import Optional

from PIL import Image

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
    QAction, QKeySequence, QShortcut, QPalette, QTransform,
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
        qimg = QImage(img.tobytes(), img.width, img.height, img.width * 3, QImage.Format_RGB888)
    elif img.mode == "RGBA":
        qimg = QImage(img.tobytes(), img.width, img.height, img.width * 4, QImage.Format_RGBA8888)
    else:
        img = img.convert("RGBA")
        qimg = QImage(img.tobytes(), img.width, img.height, img.width * 4, QImage.Format_RGBA8888)
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
    rename_requested = Signal(int)
    move_up_requested = Signal(int)
    move_down_requested = Signal(int)
    toggle_disabled = Signal(int, bool)  # (idx, disabled)

    def __init__(self, idx: int, scanner: ScannerInfo, parent=None):
        super().__init__(parent)
        self.idx = idx
        self.scanner = scanner
        self._selected = False
        self._hovered = False

        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setCursor(Qt.PointingHandCursor)
        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName("scannerCard")

        # 阴影
        add_shadow(self, blur=16, offset=2, color=QColor(0, 0, 0, 20))

        self._setup_ui()
        self._update_style()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        # 状态指示器
        self.status_dot = QLabel("●")
        self.status_dot.setFixedWidth(12)
        self.status_dot.setStyleSheet(f"color: {COLORS['success']}; font-size: 12px;")
        layout.addWidget(self.status_dot)

        # 名称和信息
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        name = _label_for(self.scanner)
        self.name_label = QLabel(name)
        self.name_label.setFont(QFont(_FONT_FAMILY, 10, QFont.Bold))
        self.name_label.setStyleSheet(f"color: {COLORS['text_primary']};")
        self.name_label.setWordWrap(True)
        self.name_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        info_layout.addWidget(self.name_label)

        ip_text = _ip_label_for(self.scanner)
        self.ip_label = QLabel(ip_text)
        self.ip_label.setFont(QFont(_FONT_FAMILY, 8))
        self.ip_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        self.ip_label.setWordWrap(True)
        self.ip_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        info_layout.addWidget(self.ip_label)

        layout.addLayout(info_layout, 1)

        # 功能标签列（竖向排列，节省横向空间）
        tag_layout = QVBoxLayout()
        tag_layout.setSpacing(2)

        if self.scanner.has_adf:
            t = QLabel("ADF")
            t.setFont(QFont(_FONT_FAMILY, 7))
            t.setStyleSheet(
                f"color: {COLORS['primary']}; background: {COLORS['primary_bg']};"
                f"padding: 1px 5px; border-radius: {RADIUS['sm']}px;"
            )
            t.setAlignment(Qt.AlignCenter)
            tag_layout.addWidget(t)

        if self.scanner.has_duplex:
            t = QLabel("双面")
            t.setFont(QFont(_FONT_FAMILY, 7))
            t.setStyleSheet(
                f"color: {COLORS['primary']}; background: {COLORS['primary_bg']};"
                f"padding: 1px 5px; border-radius: {RADIUS['sm']}px;"
            )
            t.setAlignment(Qt.AlignCenter)
            tag_layout.addWidget(t)

        if self.scanner.escl_url:
            t = QLabel("eSCL")
            t.setFont(QFont(_FONT_FAMILY, 7))
            t.setStyleSheet(
                f"color: {COLORS['success']}; background: #ECFDF5;"
                f"padding: 1px 5px; border-radius: {RADIUS['sm']}px;"
            )
            t.setAlignment(Qt.AlignCenter)
            tag_layout.addWidget(t)

        if tag_layout.count() > 0:
            layout.addLayout(tag_layout)

        # 上下移动按钮
        move_layout = QVBoxLayout()
        move_layout.setSpacing(2)
        self.up_btn = QPushButton("▲")
        self.up_btn.setFixedSize(20, 20)
        self.up_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {COLORS['text_secondary']};
                border: none;
                font-size: 10px;
            }}
            QPushButton:hover {{
                color: {COLORS['primary']};
            }}
        """)
        self.up_btn.clicked.connect(lambda: self.move_up_requested.emit(self.idx))
        move_layout.addWidget(self.up_btn)

        self.down_btn = QPushButton("▼")
        self.down_btn.setFixedSize(20, 20)
        self.down_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {COLORS['text_secondary']};
                border: none;
                font-size: 10px;
            }}
            QPushButton:hover {{
                color: {COLORS['primary']};
            }}
        """)
        self.down_btn.clicked.connect(lambda: self.move_down_requested.emit(self.idx))
        move_layout.addWidget(self.down_btn)

        layout.addLayout(move_layout)

        # 停用开关
        self.disable_btn = QPushButton("⏻")
        self.disable_btn.setFixedSize(22, 22)
        self.disable_btn.setToolTip("停用 / 启用此扫描仪")
        self.disable_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {COLORS['text_muted']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
                font-size: 10px;
            }}
            QPushButton:hover {{
                color: {COLORS['accent']};
                border-color: {COLORS['accent']};
            }}
        """)
        self.disable_btn.clicked.connect(self._on_toggle)
        self._disabled = False
        layout.addWidget(self.disable_btn)

    def _on_toggle(self):
        self._disabled = not self._disabled
        self.toggle_disabled.emit(self.idx, self._disabled)
        self._update_disable_style()

    def _update_disable_style(self):
        if self._disabled:
            self.disable_btn.setText("▶")
            self.disable_btn.setToolTip("已停用，点击启用")
            self.setStyleSheet(f"""
                ScannerCard {{
                    background: {COLORS['surface']};
                    border: 2px solid {COLORS['border']};
                    border-radius: {RADIUS['lg']}px;
                    opacity: 0.5;
                }}
            """)
        else:
            self.disable_btn.setText("⏻")
            self.disable_btn.setToolTip("停用此扫描仪")
            self._update_style()

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

    def mouseDoubleClickEvent(self, event):
        self.rename_requested.emit(self.idx)
        super().mouseDoubleClickEvent(event)

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
        transform = QTransform()
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
        self._original_geometry = None
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

    def setGeometry(self, rect):
        """重写 setGeometry 保存原始位置"""
        super().setGeometry(rect)
        if self._original_geometry is None:
            self._original_geometry = rect

    def mousePressEvent(self, event):
        # 点击缩放动画 - 使用原始 geometry 作为基准
        if self._original_geometry is None:
            self._original_geometry = self.geometry()

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(ANIM["fast"])
        self._anim.setStartValue(self._original_geometry)
        orig = self._original_geometry
        self._anim.setEndValue(QRect(orig.x() + 1, orig.y() + 1, orig.width() - 2, orig.height() - 2))
        self._anim.setEasingCurve(QEasingCurve.OutQuad)
        self._anim.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        # 恢复动画 - 恢复到原始 geometry
        if self._original_geometry is None:
            self._original_geometry = self.geometry()

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(ANIM["fast"])
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(self._original_geometry)
        self._anim.setEasingCurve(QEasingCurve.OutQuad)
        self._anim.start()
        super().mouseReleaseEvent(event)


# ────────── 启动加载界面 ──────────
class LoadingDialog(QDialog):
    """启动加载界面：覆盖在主窗口上，显示初始化进度"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HP Scan Tool")
        self.setFixedSize(420, 220)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

        # 背景色
        self.setStyleSheet(f"""
            QDialog {{
                background: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: {RADIUS['lg']}px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(16)

        # 标题
        title = QLabel("HP Scan Tool v4.0")
        title.setFont(QFont(_FONT_FAMILY, 18, QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['text_primary']};")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # 当前步骤
        self.step_label = QLabel("正在初始化...")
        self.step_label.setFont(QFont(_FONT_FAMILY, 11))
        self.step_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        self.step_label.setAlignment(Qt.AlignCenter)
        self.step_label.setMinimumHeight(24)
        layout.addWidget(self.step_label)

        # 进度条
        self.progress = QProgressBar()
        self.progress.setMinimumHeight(10)
        self.progress.setMaximumHeight(10)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)  # 不确定模式
        self.progress.setStyleSheet(f"""
            QProgressBar {{
                background: #F1F5F9;
                border: none;
                border-radius: 5px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                            stop:0 {COLORS['primary']},
                                            stop:1 {COLORS['primary_light']});
                border-radius: 5px;
            }}
        """)
        layout.addWidget(self.progress)

        # 详细状态
        self.detail_label = QLabel("")
        self.detail_label.setFont(QFont(_FONT_FAMILY, 9))
        self.detail_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        self.detail_label.setAlignment(Qt.AlignCenter)
        self.detail_label.setMinimumHeight(18)
        layout.addWidget(self.detail_label)

        layout.addStretch()

    def set_step(self, text: str):
        """设置当前步骤文本（仅主线程调用）"""
        self.step_label.setText(text)

    def set_detail(self, text: str):
        """设置详细信息（仅主线程调用）"""
        self.detail_label.setText(text)

    def set_determinate(self, maximum: int):
        """切换为确定进度模式（仅主线程调用）"""
        self.progress.setRange(0, maximum)
        self.progress.setValue(0)

    def set_value(self, value: int):
        """更新进度值（仅主线程调用）"""
        self.progress.setValue(value)


# ────────── 扫描工作线程（QThread + Signal，彻底解决跨线程问题）─────
class ScanWorker(QThread):
    """扫描工作线程：纯 QThread，通过 Signal 与主线程通信"""
    finished_signal = Signal(object)  # (data_bytes, ext) | None=失败
    error_signal = Signal(str)
    progress_signal = Signal(str)

    def __init__(self, scanner, resolution, color_mode, output_format,
                 source, duplex, cancel_event):
        super().__init__()
        self.scanner = scanner
        self.resolution = resolution
        self.color_mode = color_mode
        self.output_format = output_format
        self.source = source
        self.duplex = duplex
        self.cancel_event = cancel_event

    def run(self):
        try:
            from escl_engine import execute_scan
            data, ext = execute_scan(
                scanner=self.scanner, resolution=self.resolution,
                color_mode=self.color_mode, output_format=self.output_format,
                source=self.source, duplex=self.duplex,
                cancel_event=self.cancel_event,
            )
            if data and not self.cancel_event.is_set():
                self.finished_signal.emit((data, ext))
            else:
                self.finished_signal.emit(None)
        except Exception as e:
            self.error_signal.emit(str(e))
        finally:
            self.finished_signal.emit(None)


class MultiPageScanWorker(QThread):
    """多页扫描工作线程"""
    finished_signal = Signal(object)  # [(data, ext), ...] or []
    error_signal = Signal(str)
    progress_signal = Signal(str, int)  # (msg, page_num)

    def __init__(self, scanner, resolution, color_mode, output_format,
                 source, duplex, cancel_event):
        super().__init__()
        self.scanner = scanner
        self.resolution = resolution
        self.color_mode = color_mode
        self.output_format = output_format
        self.source = source
        self.duplex = duplex
        self.cancel_event = cancel_event

    def run(self):
        try:
            from escl_engine import execute_multipage_scan
            pages = execute_multipage_scan(
                scanner=self.scanner, resolution=self.resolution,
                color_mode=self.color_mode, output_format=self.output_format,
                source=self.source, duplex=self.duplex,
                cancel_event=self.cancel_event,
                progress_callback=lambda n, _: self.progress_signal.emit(
                    f"已扫描 {n} 页", n),
            )
            if self.cancel_event.is_set():
                self.finished_signal.emit([])
            else:
                self.finished_signal.emit(pages if pages else [])
        except Exception as e:
            self.error_signal.emit(str(e))


# ────────── 主窗口 ──────────
class ScanApp(QMainWindow):
    """主窗口（UI 优化版）"""

    def __init__(self):
        super().__init__()

        # 业务逻辑层（快速）
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
        self.duplex_val = self.cfg.get("duplex", False)
        self.direct_save_val = self.cfg.get("direct_save", False)
        self.extra_dirs = self.cfg.get("extra_dirs", [])
        self.disabled_ips = set(self.cfg.get("disabled_ips", []))
        self.cards = []

        # 先构建界面（隐藏状态）
        self._setup_ui()
        self._refresh_extra_dirs()

        # 显示加载界面（居中到屏幕）
        self._loading = LoadingDialog(self)
        self._loading.set_step("正在初始化...")
        self._loading.set_detail("加载配置")
        try:
            screen = QApplication.primaryScreen()
            if screen:
                sg = screen.geometry()
                self._loading.move(
                    sg.center().x() - self._loading.width() // 2,
                    sg.center().y() - self._loading.height() // 2,
                )
        except Exception:
            pass
        self._loading.show()

        # 延迟 100ms 确保加载界面渲染后再开始设备探测
        QTimer.singleShot(100, self._do_startup_scan)

    def _setup_ui(self):
        self.setWindowTitle("HP Scan Tool v4.0")
        self.setMinimumSize(900, 620)

        # 恢复窗口大小和位置
        width = self.cfg.get("window_width", 1050)
        height = self.cfg.get("window_height", 720)
        x = self.cfg.get("window_x")
        y = self.cfg.get("window_y")
        self.resize(width, height)
        if x is not None and y is not None:
            self.move(x, y)

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

        # 进度条（嵌入状态栏）
        self.scan_progress = QProgressBar()
        self.scan_progress.setMaximumWidth(200)
        self.scan_progress.setMaximumHeight(16)
        self.scan_progress.setVisible(False)
        self.status_bar.addPermanentWidget(self.scan_progress)

        # 淡入动画
        add_fade_animation(self, ANIM["slow"])

    # ── 启动流程（由 LoadingDialog 驱动）──
    def _do_startup_scan(self):
        """逐步执行启动流程：恢复设备 → 探测能力 → 进入主界面"""
        # 抑制 SSL 警告（探测时大量 verify=False 请求）
        try:
            import urllib3; urllib3.disable_warnings()
        except Exception:
            pass
        try:
            import warnings; warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        except Exception:
            pass

        # 第1步：检查是否有已保存的扫描仪
        saved = self.coordinator.restore_saved_scanners()
        if not saved:
            QTimer.singleShot(0, self._finish_loading)
            return

        # 第2步：逐个探测设备能力
        self._loading.set_step("正在探测扫描仪...")
        self._loading.set_determinate(len(saved))
        self._rebuild_cards()

        # 线程间共享的进度状态（后台线程只写，主线程轮询读）
        self._probe_state = {
            "current": 0, "total": len(saved),
            "detail": "", "done": False, "errors": [],
        }

        # 主线程轮询器：每 100ms 读进度并更新 UI
        def _poll():
            s = self._probe_state
            if s["done"]:
                self._poll_timer.stop()
                QTimer.singleShot(100, self._on_startup_complete)
                return
            self._loading.set_detail(s["detail"])
            self._loading.set_value(s["current"])

        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(_poll)
        self._poll_timer.start(100)

        # 看门狗：15s 后强制完成
        QTimer.singleShot(15000, self._on_probe_watchdog)

        # 后台线程：只做网络请求，绝不碰 UI
        def _probe_thread():
            from escl_engine import fetch_capabilities, probe_escl
            for idx, scanner in enumerate(saved):
                self._probe_state["current"] = idx
                self._probe_state["detail"] = (
                    f"正在探测 {scanner.display_name} ({idx + 1}/{len(saved)})")
                try:
                    # 先探测 eSCL URL（restore 回来的 scanner 没有 escl_url）
                    if not scanner.escl_url and scanner.ip:
                        url = probe_escl(scanner.ip, timeout=4.0)
                        if url:
                            scanner.escl_url = url
                    # 再获取设备能力
                    if scanner.escl_url:
                        fetch_capabilities(scanner, timeout=4.0)
                except Exception as e:
                    self._probe_state["errors"].append((scanner.display_name, str(e)))
                self._probe_state["current"] = idx + 1
            self._probe_state["done"] = True

        threading.Thread(target=_probe_thread, daemon=True).start()

    def _on_probe_watchdog(self):
        """看门狗触发：强制结束等待"""
        if hasattr(self, '_probe_state') and not self._probe_state.get("done"):
            self._probe_state["done"] = True
            self._loading.set_detail("部分设备超时，跳过...")

    def _finish_loading(self):
        """无保存设备时：加载界面等待自动发现（最多20秒）"""
        self._loading.set_step("正在发现扫描仪...")
        self._loading.set_detail("通过 mDNS/WSD 搜索网络中的打印机")
        self._startup_discover = True

        # 看门狗：20秒后强制进入（即使没找到设备）
        QTimer.singleShot(20000, self._on_startup_discover_watchdog)

        # 启动自动发现
        self._auto_discover()

    def _on_startup_complete(self):
        """启动流程完成，进入主界面"""
        if hasattr(self, '_poll_timer'):
            self._poll_timer.stop()

        errors = self._probe_state.get("errors", [])
        self._rebuild_cards()

        if self.coordinator.scanners and self.selected_idx < 0:
            self._on_card_clicked(0)

        if self._loading:
            self._loading.close()
            self._loading = None
        self.show()

        count = len(self.coordinator.scanners)
        msg = f"就绪 — {count} 台扫描仪"
        if errors:
            msg += f"（{len(errors)} 台探测失败）"
        self.status_bar.showMessage(msg)

        # 不再自动发起发现（避免覆盖扫描状态提示）

    def _build_scanner_panel(self, parent_layout):
        """构建左侧扫描仪列表面板"""
        panel = QFrame()
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(340)
        panel.setObjectName("scannerPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(SPACING["md"], SPACING["md"], SPACING["md"], SPACING["md"])
        layout.setSpacing(SPACING["sm"])

        # 标题
        title = QLabel("扫描仪")
        title.setFont(QFont(_FONT_FAMILY, 13, QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['text_primary']};")
        title.setMinimumHeight(28)
        layout.addWidget(title)

        # 按钮行
        btn_row = QHBoxLayout()
        refresh_btn = AnimatedButton("刷新", button_type="secondary")
        refresh_btn.setMinimumHeight(32)
        refresh_btn.clicked.connect(self._auto_discover)
        btn_row.addWidget(refresh_btn)

        add_btn = AnimatedButton("手动添加", button_type="secondary")
        add_btn.setMinimumHeight(32)
        add_btn.clicked.connect(self._show_add_printer_dialog)
        btn_row.addWidget(add_btn)

        layout.addLayout(btn_row)

        # 扫描仪卡片滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("border: none; background: transparent;")
        scroll.setMinimumWidth(240)

        self.cards_container = QWidget()
        self.cards_container.setMinimumWidth(230)
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(SPACING["sm"])
        self.cards_layout.addStretch(1)
        scroll.setWidget(self.cards_container)
        layout.addWidget(scroll)

        # 停用设备计数标签（始终可见）
        self._disabled_info = QLabel("已停用: 0 台")
        self._disabled_info.setFont(QFont(_FONT_FAMILY, 9))
        self._disabled_info.setStyleSheet(f"color: {COLORS['text_muted']}; padding: 2px 0;")
        self._disabled_info.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._disabled_info)

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
        self.resolution_combo = self._create_styled_combo(["150", "200", "300", "600", "1200"])
        self.resolution_combo.setCurrentText(str(self.resolution_val))
        self.resolution_combo.currentTextChanged.connect(self._on_resolution_changed)
        params_layout.addWidget(self.resolution_combo, 0, 1)

        # 颜色模式
        params_layout.addWidget(QLabel("颜色:"), 1, 0)
        self.color_mode_combo = self._create_styled_combo(["彩色", "灰度", "黑白"])
        self.color_mode_combo.setCurrentText(self.color_mode_val)
        self.color_mode_combo.currentTextChanged.connect(self._on_color_mode_changed)
        params_layout.addWidget(self.color_mode_combo, 1, 1)

        # 输出格式
        params_layout.addWidget(QLabel("格式:"), 2, 0)
        self.format_combo = self._create_styled_combo(["jpg", "png", "pdf", "tiff"])
        self.format_combo.setCurrentText(self.output_format_val)
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        params_layout.addWidget(self.format_combo, 2, 1)

        # 来源
        params_layout.addWidget(QLabel("来源:"), 3, 0)
        self.source_combo = self._create_styled_combo(["平板"])
        self.source_combo.setCurrentText(self.source_val)
        self.source_combo.currentTextChanged.connect(self._on_source_changed)
        params_layout.addWidget(self.source_combo, 3, 1)

        # 双面扫描
        self.duplex_check = QCheckBox("双面扫描")
        self.duplex_check.stateChanged.connect(self._on_duplex_changed)
        params_layout.addWidget(self.duplex_check, 4, 0, 1, 2)

        # 直接保存（跳过预览）
        self.direct_save_check = QCheckBox("直接保存（跳过预览）")
        self.direct_save_check.stateChanged.connect(self._on_direct_save_changed)
        params_layout.addWidget(self.direct_save_check, 5, 0, 1, 2)

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

        self.listen_btn = AnimatedButton("监听模式", button_type="accent")
        self.listen_btn.clicked.connect(self._start_listen)
        btn_layout.addWidget(self.listen_btn)

        history_btn = AnimatedButton("扫描历史", button_type="secondary")
        history_btn.clicked.connect(self._show_history)
        btn_layout.addWidget(history_btn)

        layout.addLayout(btn_layout)

        # 第二行按钮
        btn_row2 = QHBoxLayout()

        wia_btn = AnimatedButton("USB 扫描", button_type="secondary")
        wia_btn.clicked.connect(self._wia_scan)
        btn_row2.addWidget(wia_btn)

        status_btn = AnimatedButton("打印机状态", button_type="secondary")
        status_btn.clicked.connect(self._show_scanner_status)
        btn_row2.addWidget(status_btn)

        cache_btn = AnimatedButton("缓存设置", button_type="secondary")
        cache_btn.clicked.connect(self._show_cache_settings)
        btn_row2.addWidget(cache_btn)

        profile_btn = AnimatedButton("配置管理", button_type="secondary")
        profile_btn.clicked.connect(self._show_profile_manager)
        btn_row2.addWidget(profile_btn)

        layout.addLayout(btn_row2)
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

    def _create_styled_combo(self, items):
        """创建统一样式的下拉框（修复双层叠加问题）"""
        combo = QComboBox()
        combo.addItems(items)
        combo.setMinimumHeight(32)
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # 关键修复：使用自定义视图避免双层叠加
        combo.setStyleSheet(f"""
            QComboBox {{
                background: #FFFFFF;
                border: 1.5px solid #E2E8F0;
                border-radius: 8px;
                padding: 8px 12px;
                color: #1E293B;
            }}
            QComboBox:hover {{
                border-color: #0D9488;
            }}
            QComboBox:focus {{
                border-color: #0D9488;
                background: #F0FDFA;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 24px;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #64748B;
                margin-right: 8px;
            }}
            QComboBox QAbstractItemView {{
                background: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
                padding: 6px;
                outline: none;
            }}
            QComboBox QAbstractItemView::item {{
                padding: 10px 14px;
                border-radius: 6px;
                color: #1E293B;
                margin: 2px 4px;
            }}
            QComboBox QAbstractItemView::item:hover {{
                background: #F0FDFA;
                color: #0D9488;
            }}
            QComboBox QAbstractItemView::item:selected {{
                background: #0D9488;
                color: white;
            }}
        """)
        return combo

    def _on_duplex_changed(self, state):
        self.duplex_val = bool(state)
        self.cfg["duplex"] = self.duplex_val
        save_config(self.cfg)

    def _on_direct_save_changed(self, state):
        self.direct_save_val = bool(state)
        self.cfg["direct_save"] = self.direct_save_val
        save_config(self.cfg)

    def _update_duplex_visibility(self):
        """根据来源更新双面复选框可见性（自动模式也视作可能使用ADF）"""
        use_adf = self.source_val in ("ADF", "自动")
        has_duplex = self.selected and self.selected.has_duplex
        self.duplex_check.setVisible(use_adf and has_duplex)
        if not use_adf:
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
            self.cfg["extra_dirs"] = self.extra_dirs
            save_config(self.cfg)
            self._refresh_extra_dirs()

    def _clear_extra_dirs(self):
        self.extra_dirs.clear()
        self.cfg["extra_dirs"] = self.extra_dirs
        save_config(self.cfg)
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
        # 防止重复点击
        if hasattr(self, '_discovering') and self._discovering:
            return
        self._discovering = True

        self.status_bar.showMessage("正在发现扫描仪...")

        def _on_complete(new_count, new_scanners):
            self._discovering = False
            QTimer.singleShot(0, self._on_discover_finished)

        self.coordinator.discover_scanners_background(_on_complete)

    def _on_startup_discover_watchdog(self):
        """启动发现看门狗：超时后强制进入"""
        if getattr(self, '_startup_discover', False):
            self._startup_discover = False
            if self._loading:
                self._loading.close()
                self._loading = None
            self.show()
            self.status_bar.showMessage("未发现扫描仪，请点击「刷新」或「手动添加」")

    def _on_discover_finished(self):
        """发现完成更新 UI"""
        self._rebuild_cards()
        count = len(self.coordinator.scanners)
        self.status_bar.showMessage(f"发现 {count} 台扫描仪")

        # 启动发现模式：关闭加载界面，显示主窗口
        if getattr(self, '_startup_discover', False):
            self._startup_discover = False
            if self.coordinator.scanners:
                self._on_card_clicked(0)
            if self._loading:
                self._loading.close()
                self._loading = None
            self.show()

    def _rebuild_cards(self):
        """重建扫描仪卡片列表（停用设备半透明置底）"""
        for card in self.cards:
            card.deleteLater()
        self.cards.clear()

        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.spacerItem():
                self.cards_layout.removeItem(item)

        active_scanners = []
        disabled_scanners = []
        for idx, scanner in enumerate(self.coordinator.scanners):
            is_disabled = scanner.ip in self.disabled_ips
            card = ScannerCard(idx, scanner)
            card.clicked.connect(self._on_card_clicked)
            card.rename_requested.connect(self._on_rename_requested)
            card.move_up_requested.connect(self._on_move_up_requested)
            card.move_down_requested.connect(self._on_move_down_requested)
            card.toggle_disabled.connect(self._on_toggle_disabled)
            self.cards.append(card)
            if is_disabled:
                # 恢复停用状态（卡片重建时 _disabled 被重置）
                card._disabled = True
                card.disable_btn.setText("▶")
                card.disable_btn.setToolTip("已停用，点击启用")
                card.setStyleSheet(f"""
                    ScannerCard {{
                        background: {COLORS['surface']};
                        border: 1.5px dashed {COLORS['border']};
                        border-radius: {RADIUS['lg']}px;
                    }}
                """)
                card.setGraphicsEffect(QGraphicsOpacityEffect(opacity=0.55))
                disabled_scanners.append(card)
            else:
                active_scanners.append(card)

        # 活跃设备在上，停用设备在下方
        for card in active_scanners:
            self.cards_layout.addWidget(card)
        for card in disabled_scanners:
            self.cards_layout.addWidget(card)

        self.cards_layout.addStretch(1)

        self.cards_layout.addStretch(1)

        # 更新停用计数
        cnt = len([s for s in self.coordinator.scanners if s.ip in self.disabled_ips])
        if hasattr(self, '_disabled_info'):
            self._disabled_info.setText(f"已停用: {cnt} 台")

    def _on_toggle_disabled(self, idx: int, disabled: bool):
        """停用/启用扫描仪"""
        if 0 <= idx < len(self.coordinator.scanners):
            scanner = self.coordinator.scanners[idx]
            if disabled:
                self.disabled_ips.add(scanner.ip)
            else:
                self.disabled_ips.discard(scanner.ip)
            self.cfg["disabled_ips"] = list(self.disabled_ips)
            save_config(self.cfg)
            self._rebuild_cards()
            self.status_bar.showMessage(f"{'停用' if disabled else '启用'}: {scanner.display_name}")

    def _on_card_clicked(self, idx: int):
        """点击扫描仪卡片"""
        if 0 <= idx < len(self.coordinator.scanners):
            scanner = self.coordinator.scanners[idx]
            # 停用设备不响应选择点击（但启用按钮仍可用）
            if scanner.ip in self.disabled_ips:
                return
            self.selected = scanner
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

    def _on_rename_requested(self, idx: int):
        """重命名扫描仪"""
        if 0 <= idx < len(self.coordinator.scanners):
            scanner = self.coordinator.scanners[idx]
            self._show_rename_dialog(scanner)

    def _show_rename_dialog(self, scanner: ScannerInfo):
        """显示重命名对话框"""
        dialog = QDialog(self)
        dialog.setWindowTitle("重命名设备")
        dialog.setMinimumWidth(300)
        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel("设备名称:"))
        name_input = QLineEdit(scanner.custom_name or scanner.display_name)
        name_input.selectAll()
        layout.addWidget(name_input)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        save_btn = AnimatedButton("保存", button_type="primary")
        save_btn.clicked.connect(dialog.accept)
        btn_layout.addWidget(save_btn)

        cancel_btn = AnimatedButton("取消", button_type="secondary")
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

        if dialog.exec() == QDialog.Accepted:
            new_name = name_input.text().strip()
            if new_name:
                scanner.custom_name = new_name
                # 保存到配置
                self.coordinator.set_custom_name(scanner.ip, new_name)
                self._rebuild_cards()
                self.status_bar.showMessage(f"已重命名为: {new_name}")

    def _on_move_up_requested(self, idx: int):
        """设备上移"""
        if idx > 0 and idx < len(self.coordinator.scanners):
            scanners = self.coordinator.scanners
            scanners[idx], scanners[idx - 1] = scanners[idx - 1], scanners[idx]
            self._rebuild_cards()
            # 更新选中
            self.selected_idx = idx - 1
            self.selected = scanners[self.selected_idx]
            for i, card in enumerate(self.cards):
                card.set_selected(i == self.selected_idx)

    def _on_move_down_requested(self, idx: int):
        """设备下移"""
        if idx >= 0 and idx < len(self.coordinator.scanners) - 1:
            scanners = self.coordinator.scanners
            scanners[idx], scanners[idx + 1] = scanners[idx + 1], scanners[idx]
            self._rebuild_cards()
            # 更新选中
            self.selected_idx = idx + 1
            self.selected = scanners[self.selected_idx]
            for i, card in enumerate(self.cards):
                card.set_selected(i == self.selected_idx)

    def _update_source_options(self, has_adf: bool, has_duplex: bool):
        """更新来源选项"""
        self.source_combo.clear()
        self.source_combo.addItem("平板")
        if has_adf:
            self.source_combo.addItem("ADF")
            self.source_combo.addItem("自动")  # 自动检测输稿器状态
            self.source_combo.setCurrentText("自动")  # 有ADF时默认自动
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
        """开始扫描（自动检测 ADF 状态）"""
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
        source_text = self.source_combo.currentText()
        if source_text == "ADF":
            source_val = "Feeder"
        else:
            # "平板" 和 "自动" 都走平板单页扫描
            source_val = "Platen"

        self.scan_btn.setText("扫描中...")
        self.scan_btn.setEnabled(False)
        self.status_bar.showMessage("正在扫描...")
        self.scan_progress.setVisible(True)
        self.scan_progress.setRange(0, 0)

        # 取消标志
        self._scan_cancel_event = threading.Event()

        # 添加取消按钮
        self.cancel_scan_btn = AnimatedButton("取消扫描", button_type="accent")
        self.cancel_scan_btn.clicked.connect(self._cancel_scan)
        self.status_bar.addPermanentWidget(self.cancel_scan_btn)

        # 决定来源
        actual_source, use_adf = self._detect_adf_source(scanner, source_val)
        if use_adf:
            self.status_bar.showMessage("ADF扫描：输稿器自动进纸，请稍候...")
            self._start_multipage_worker(scanner, out_dir, actual_source)
        else:
            self.status_bar.showMessage("平板扫描进行中，约5-10秒完成...")
            self._start_single_worker(scanner, out_dir, actual_source)

    def _start_single_worker(self, scanner, out_dir, source_val):
        """启动单页扫描 QThread"""
        self._worker = ScanWorker(
            scanner, self.resolution_val,
            COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
            self.output_format_val, source_val, self.duplex_val,
            self._scan_cancel_event,
        )

        def _on_result(result):
            self._worker = None
            if result is None or not result[0]:
                self.status_bar.showMessage("扫描取消或失败")
                self._reset_scan_ui()
                return
            data, ext = result
            job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            cache_manager.register_job(job_id)
            cache_path = cache_manager.write_page(job_id, 1, data, ext)
            del data
            self._apply_post_scan_fixups(cache_path)
            output_path = os.path.join(out_dir, f"HP_Scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")
            self._reset_scan_ui()
            self._show_preview(cache_path, ext, output_path, job_id, scanner)

        self._worker.finished_signal.connect(_on_result)
        self._worker.error_signal.connect(lambda e: self._scan_error(f"扫描失败: {e}"))
        self._worker.start()

    def _start_multipage_worker(self, scanner, out_dir, source_val):
        """启动多页扫描 QThread"""
        self._worker = MultiPageScanWorker(
            scanner, self.resolution_val,
            COLOR_MODE_MAP.get(self.color_mode_val, "RGB24"),
            self.output_format_val, source_val, self.duplex_val,
            self._scan_cancel_event,
        )

        def _on_result(pages):
            self._worker = None
            if not pages:
                self.status_bar.showMessage("扫描取消或失败")
                self._reset_scan_ui()
                return
            job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            cache_manager.register_job(job_id)
            ext = pages[0][1] if pages else self.output_format_val
            for i, (data, ext) in enumerate(pages, 1):
                cache_path = cache_manager.write_page(job_id, i, data, ext)
                del data
                self._apply_post_scan_fixups(cache_path)
            output_path = os.path.join(out_dir, f"HP_Scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")
            self._reset_scan_ui()
            self._show_multi_preview(job_id, ext, output_path, len(pages), scanner)

        self._worker.finished_signal.connect(_on_result)
        self._worker.error_signal.connect(lambda e: self._scan_error(f"扫描失败: {e}"))
        self._worker.start()

    def _detect_adf_source(self, scanner, user_source):
        """根据用户选择决定扫描来源"""
        if user_source == "Feeder" and scanner.has_adf:
            QTimer.singleShot(0, lambda: self.status_bar.showMessage("ADF 输稿器扫描"))
            return "Feeder", True
        QTimer.singleShot(0, lambda: self.status_bar.showMessage("平板扫描"))
        return "Platen", False

    def _cancel_scan(self):
        """取消扫描"""
        if hasattr(self, '_scan_cancel_event'):
            self._scan_cancel_event.set()
        # 停止QThread worker（如果还在运行）
        if hasattr(self, '_worker') and self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(3000)
            self._worker = None
        self.status_bar.showMessage("扫描已取消")
        self._reset_scan_ui()

    def _apply_post_scan_fixups(self, cache_path):
        """扫描后处理：A4 裁剪（居中，不超过原图）"""
        try:
            from escl_engine import crop_to_a4
            with open(cache_path, "rb") as f:
                raw = f.read()
            img = Image.open(io.BytesIO(raw))
            cropped = crop_to_a4(img, dpi=self.resolution_val)
            if cropped.size != img.size:
                buf = io.BytesIO()
                cropped.convert("RGB").save(buf, format="JPEG")
                with open(cache_path, "wb") as f:
                    f.write(buf.getvalue())
        except Exception:
            pass

    def _safe_copy_file(self, src, dst, retries=3, delay=0.5):
        """安全复制文件，带重试机制"""
        for attempt in range(retries):
            try:
                shutil.copy2(src, dst)
                return True
            except (IOError, OSError) as e:
                if attempt < retries - 1:
                    time.sleep(delay)
                else:
                    raise
        return False

    def _direct_save(self, cache_path, ext, output_path, job_id, scanner=None):
        """直接保存（跳过预览）"""
        try:
            img = Image.open(cache_path)
            if ext == "pdf":
                pdf_path = output_path if output_path.lower().endswith(".pdf") \
                           else output_path.rsplit(".", 1)[0] + ".pdf"
                img.convert("RGB").save(pdf_path, "PDF", resolution=300.0)
                saved = pdf_path
            else:
                final = output_path
                if not final.lower().endswith(f".{ext}"):
                    final = f"{final.rsplit('.', 1)[0]}.{ext}"
                if ext in ("jpg", "jpeg"):
                    img.convert("RGB").save(final, "JPEG", quality=95)
                elif ext == "png":
                    img.save(final, "PNG")
                elif ext in ("tiff", "tif"):
                    img.save(final, "TIFF")
                saved = final

            # 复制到额外输出目录
            for extra_dir in self.extra_dirs:
                try:
                    os.makedirs(extra_dir, exist_ok=True)
                    extra_path = os.path.join(extra_dir, os.path.basename(saved))
                    if extra_path != saved:
                        self._safe_copy_file(saved, extra_path)
                except Exception:
                    pass

            cache_manager.remove_job(job_id)

            history_manager.append({
                "device_name": scanner.model if scanner else "",
                "device_ip": scanner.ip if scanner else "",
                "page_count": 1,
                "format": ext,
                "file_path": saved,
            })

            self.status_bar.showMessage(f"已保存: {os.path.basename(saved)}")
            self._reset_scan_ui()

            try:
                os.startfile(os.path.dirname(saved))
            except Exception:
                pass

        except Exception as e:
            self._scan_error(f"保存失败: {e}")

    def _direct_save_multipage(self, job_id, ext, output_path, page_count, scanner=None):
        """多页直接保存（跳过预览）"""
        try:
            base_path = output_path.rsplit(".", 1)[0]
            saved_files = []

            if ext == "pdf":
                pdf_images = []
                for i in range(1, page_count + 1):
                    try:
                        cache_path = os.path.join(
                            cache_manager._job_dir(job_id), f"page_{i:03d}.{ext}")
                        if os.path.exists(cache_path):
                            img = Image.open(cache_path)
                            pdf_images.append(img.convert("RGB"))
                    except Exception:
                        pass
                if pdf_images:
                    pdf_path = f"{base_path}.pdf"
                    pdf_images[0].save(pdf_path, "PDF", save_all=True,
                                        append_images=pdf_images[1:])
                    saved_files.append(pdf_path)
            else:
                for i in range(1, page_count + 1):
                    try:
                        cache_path = os.path.join(
                            cache_manager._job_dir(job_id), f"page_{i:03d}.{ext}")
                        if os.path.exists(cache_path):
                            fpath = f"{base_path}_{i:03d}.{ext}"
                            if ext in ("jpg", "jpeg"):
                                shutil.copy2(cache_path, fpath)
                            else:
                                img = Image.open(cache_path)
                                if ext == "png":
                                    img.save(fpath, "PNG")
                                elif ext in ("tiff", "tif"):
                                    img.save(fpath, "TIFF")
                                else:
                                    if img.mode != "RGB":
                                        img = img.convert("RGB")
                                    img.save(fpath, "JPEG", quality=95)
                            saved_files.append(fpath)
                    except Exception:
                        pass

            # 复制到额外输出目录
            for extra_dir in self.extra_dirs:
                try:
                    os.makedirs(extra_dir, exist_ok=True)
                    for fpath in saved_files:
                        extra_path = os.path.join(extra_dir, os.path.basename(fpath))
                        if extra_path != fpath:
                            shutil.copy2(fpath, extra_path)
                except Exception:
                    pass

            cache_manager.remove_job(job_id)

            if saved_files:
                history_manager.append({
                    "device_name": scanner.model if scanner else "",
                    "device_ip": scanner.ip if scanner else "",
                    "page_count": page_count,
                    "format": ext,
                    "file_path": saved_files[0],
                    "file_count": len(saved_files),
                })

            self.status_bar.showMessage(f"已保存 {len(saved_files)} 个文件")
            self._reset_scan_ui()

            try:
                os.startfile(os.path.dirname(saved_files[0]))
            except Exception:
                pass

        except Exception as e:
            self._scan_error(f"保存失败: {e}")

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
        self.listen_btn.setEnabled(True)
        self.scan_progress.setVisible(False)
        # 移除取消监听按钮
        if hasattr(self, 'cancel_listen_btn') and self.cancel_listen_btn:
            self.status_bar.removeWidget(self.cancel_listen_btn)
            self.cancel_listen_btn.deleteLater()
            self.cancel_listen_btn = None
        # 移除取消扫描按钮
        if hasattr(self, 'cancel_scan_btn') and self.cancel_scan_btn:
            self.status_bar.removeWidget(self.cancel_scan_btn)
            self.cancel_scan_btn.deleteLater()
            self.cancel_scan_btn = None

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
        source_text = self.source_combo.currentText()
        if source_text == "ADF":
            source_val = "Feeder"
            use_adf = True
        elif source_text == "自动":
            source_val = "Auto"
            use_adf = False  # 由 _detect_adf_source 判断
        else:
            source_val = "Platen"
            use_adf = False

        # 取消标志
        self._listen_cancel_event = threading.Event()

        self.scan_btn.setText("监听中...")
        self.scan_btn.setEnabled(False)
        self.listen_btn.setEnabled(False)
        self.status_bar.showMessage("监听模式：等待打印机面板触发扫描...")

        # 添加取消按钮
        self.cancel_listen_btn = AnimatedButton("取消监听", button_type="accent")
        self.cancel_listen_btn.clicked.connect(self._cancel_listen)
        self.status_bar.addPermanentWidget(self.cancel_listen_btn)

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
                        cancel_event=self._listen_cancel_event,
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
                        cancel_event=self._listen_cancel_event,
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

    def _cancel_listen(self):
        """取消监听模式"""
        if hasattr(self, '_listen_cancel_event'):
            self._listen_cancel_event.set()
        self.status_bar.showMessage("监听模式：已取消")
        self._reset_scan_ui()

    # ────────── 历史记录 ──────────
    def _show_history(self):
        """显示扫描历史"""
        dialog = HistoryDialog(self)
        dialog.exec()

    # ────────── 手动添加打印机 ──────────
    def _show_add_printer_dialog(self):
        """显示手动添加打印机对话框"""
        dialog = QDialog(self)
        dialog.setWindowTitle("手动添加打印机")
        dialog.setMinimumWidth(350)
        layout = QVBoxLayout(dialog)

        # IP 地址输入
        ip_layout = QHBoxLayout()
        ip_layout.addWidget(QLabel("IP 地址:"))
        ip_input = QLineEdit()
        ip_input.setPlaceholderText("例如: 192.168.1.100")
        ip_layout.addWidget(ip_input)
        layout.addLayout(ip_layout)

        # 端口输入
        port_layout = QHBoxLayout()
        port_layout.addWidget(QLabel("端口:"))
        port_input = QLineEdit("80")
        port_input.setMaximumWidth(80)
        port_layout.addWidget(port_input)
        port_layout.addStretch()
        layout.addLayout(port_layout)

        # 状态标签
        status_label = QLabel("")
        status_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(status_label)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        probe_btn = AnimatedButton("探测", button_type="secondary")
        btn_layout.addWidget(probe_btn)

        add_btn = AnimatedButton("添加", button_type="primary")
        btn_layout.addWidget(add_btn)

        cancel_btn = AnimatedButton("取消", button_type="secondary")
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

        def _probe():
            ip = ip_input.text().strip()
            if not ip:
                status_label.setText("请输入 IP 地址")
                return
            status_label.setText("正在探测...")
            probe_btn.setEnabled(False)

            def _probe_thread():
                from escl_engine import probe_escl
                url = probe_escl(ip, int(port_input.text() or 80))
                if url:
                    QTimer.singleShot(0, lambda: status_label.setText(
                        f"✓ 发现 eSCL 服务: {url}"))
                    QTimer.singleShot(0, lambda: add_btn.setEnabled(True))
                else:
                    QTimer.singleShot(0, lambda: status_label.setText(
                        "✗ 未发现 eSCL 服务"))
                QTimer.singleShot(0, lambda: probe_btn.setEnabled(True))

            threading.Thread(target=_probe_thread, daemon=True).start()

        def _add():
            ip = ip_input.text().strip()
            if not ip:
                return
            # 添加到已保存列表
            saved_ips = self.cfg.get("saved_ips", [])
            if not any(item.get("ip") == ip for item in saved_ips):
                saved_ips.append({"ip": ip, "model": "手动添加"})
                self.cfg["saved_ips"] = saved_ips
                save_config(self.cfg)
            # 恢复扫描仪
            self.coordinator.restore_saved_scanners()
            self._rebuild_cards()
            self.status_bar.showMessage(f"已添加打印机: {ip}")
            dialog.accept()

        probe_btn.clicked.connect(_probe)
        add_btn.clicked.connect(_add)
        add_btn.setEnabled(False)

        dialog.exec()

    # ────────── 扫描仪状态 ──────────
    def _show_scanner_status(self):
        """显示当前扫描仪状态"""
        if not self.selected:
            QMessageBox.warning(self, "提示", "请先选择一台打印机")
            return

        scanner = self.selected
        if not scanner.escl_url:
            QMessageBox.information(self, "提示", "该打印机不支持 eSCL 状态查询")
            return

        self.status_bar.showMessage("正在查询打印机状态...")

        def _status_thread():
            from escl_engine import get_scanner_status
            status = get_scanner_status(scanner.escl_url)
            QTimer.singleShot(0, lambda: self._on_status_received(status))

        threading.Thread(target=_status_thread, daemon=True).start()

    def _on_status_received(self, status):
        """状态查询完成"""
        if not status:
            self.status_bar.showMessage("无法获取打印机状态")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("打印机状态")
        dialog.setMinimumWidth(350)
        layout = QVBoxLayout(dialog)

        # 状态信息
        for key, value in status.items():
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{key}:"))
            value_label = QLabel(str(value))
            value_label.setWordWrap(True)
            row.addWidget(value_label, 1)
            layout.addLayout(row)

        layout.addStretch()

        close_btn = AnimatedButton("关闭", button_type="primary")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)

        dialog.exec()

    # ────────── WIA/USB 扫描 ──────────
    def _wia_scan(self):
        """WIA USB 扫描"""
        try:
            from wia_engine import list_wia_scanners, try_wia_scan
        except ImportError:
            QMessageBox.warning(self, "提示", "WIA 引擎不可用（仅 Windows 支持）")
            return

        # 获取 WIA 扫描仪列表
        wia_scanners = list_wia_scanners()
        if not wia_scanners:
            QMessageBox.information(self, "提示", "未发现 WIA/USB 扫描仪")
            return

        # 选择扫描仪
        dialog = QDialog(self)
        dialog.setWindowTitle("选择 WIA 扫描仪")
        dialog.setMinimumWidth(350)
        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel("选择 USB 扫描仪:"))
        combo = QComboBox()
        for s in wia_scanners:
            combo.addItem(s.get("name", s.get("device_id", "未知")))
        layout.addWidget(combo)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        scan_btn = AnimatedButton("扫描", button_type="primary")
        btn_layout.addWidget(scan_btn)

        cancel_btn = AnimatedButton("取消", button_type="secondary")
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

        def _do_wia_scan():
            idx = combo.currentIndex()
            if idx < 0 or idx >= len(wia_scanners):
                return
            dialog.accept()

            device = wia_scanners[idx]
            self.status_bar.showMessage("正在通过 WIA 扫描...")

            def _wia_thread():
                try:
                    data, ext = try_wia_scan(
                        device_id=device.get("device_id"),
                        resolution=self.resolution_val,
                    )
                    if data:
                        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        cache_manager.register_job(job_id)
                        cache_path = cache_manager.write_page(job_id, 1, data, ext)
                        del data
                        out_dir = self.dir_entry.text() or self.output_dir
                        output_path = os.path.join(
                            out_dir,
                            f"HP_WIA_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
                        )
                        QTimer.singleShot(0, lambda: self._show_preview(
                            cache_path, ext, output_path, job_id))
                    else:
                        QTimer.singleShot(0, lambda: self.status_bar.showMessage("WIA 扫描失败"))
                except Exception as e:
                    QTimer.singleShot(0, lambda: self._scan_error(f"WIA 错误: {e}"))

            threading.Thread(target=_wia_thread, daemon=True).start()

        scan_btn.clicked.connect(_do_wia_scan)
        dialog.exec()

    # ────────── 缓存设置 ──────────
    def _show_cache_settings(self):
        """显示缓存设置对话框"""
        dialog = QDialog(self)
        dialog.setWindowTitle("缓存设置")
        dialog.setMinimumWidth(350)
        layout = QVBoxLayout(dialog)

        # 当前缓存配置
        config = cache_manager.load_cache_config()

        # 缓存限制
        limit_layout = QHBoxLayout()
        limit_layout.addWidget(QLabel("缓存限制 (MB):"))
        limit_spin = QSpinBox()
        limit_spin.setRange(100, 5000)
        limit_spin.setValue(config.get("limit_mb", 1024))
        limit_layout.addWidget(limit_spin)
        layout.addLayout(limit_layout)

        # 触发比例
        trigger_layout = QHBoxLayout()
        trigger_layout.addWidget(QLabel("触发比例 (%):"))
        trigger_spin = QSpinBox()
        trigger_spin.setRange(50, 99)
        trigger_spin.setValue(int(config.get("trigger_ratio", 0.95) * 100))
        trigger_layout.addWidget(trigger_spin)
        layout.addLayout(trigger_layout)

        # 清理大小
        cleanup_layout = QHBoxLayout()
        cleanup_layout.addWidget(QLabel("清理大小 (MB):"))
        cleanup_spin = QSpinBox()
        cleanup_spin.setRange(50, 500)
        cleanup_spin.setValue(config.get("cleanup_mb", 200))
        cleanup_layout.addWidget(cleanup_spin)
        layout.addLayout(cleanup_layout)

        layout.addStretch()

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        save_btn = AnimatedButton("保存", button_type="primary")
        btn_layout.addWidget(save_btn)

        cancel_btn = AnimatedButton("取消", button_type="secondary")
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

        def _save():
            config["limit_mb"] = limit_spin.value()
            config["trigger_ratio"] = trigger_spin.value() / 100.0
            config["cleanup_mb"] = cleanup_spin.value()
            cache_manager.save_cache_config(config)
            self.status_bar.showMessage("缓存设置已保存")
            dialog.accept()

        save_btn.clicked.connect(_save)
        dialog.exec()

    # ────────── 窗口关闭时保存设置 ──────────
    def closeEvent(self, event):
        """关闭窗口时保存窗口大小和位置"""
        self.cfg["window_width"] = self.width()
        self.cfg["window_height"] = self.height()
        self.cfg["window_x"] = self.x()
        self.cfg["window_y"] = self.y()
        save_config(self.cfg)
        super().closeEvent(event)

    # ────────── 配置文件管理 ──────────
    def _show_profile_manager(self):
        """显示配置文件管理对话框"""
        dialog = QDialog(self)
        dialog.setWindowTitle("配置文件管理")
        dialog.setMinimumWidth(400)
        layout = QVBoxLayout(dialog)

        # 配置文件列表
        tree = QTreeWidget()
        tree.setHeaderLabels(["IP 地址", "分辨率", "颜色", "格式", "来源"])
        tree.setColumnWidth(0, 120)
        tree.setColumnWidth(1, 80)
        tree.setColumnWidth(2, 60)
        tree.setColumnWidth(3, 60)
        tree.setColumnWidth(4, 60)
        layout.addWidget(tree)

        def _refresh_profiles():
            tree.clear()
            profiles = profile_manager.get_all_profiles()
            for ip, params in profiles.items():
                item = QTreeWidgetItem([
                    ip,
                    str(params.get("resolution", "")),
                    params.get("color_mode", ""),
                    params.get("output_format", ""),
                    params.get("source", ""),
                ])
                tree.addTopLevelItem(item)

        _refresh_profiles()

        # 按钮
        btn_layout = QHBoxLayout()

        delete_btn = AnimatedButton("删除选中", button_type="secondary")
        delete_btn.clicked.connect(lambda: _delete_selected())
        btn_layout.addWidget(delete_btn)

        clear_all_btn = AnimatedButton("清空全部", button_type="accent")
        clear_all_btn.clicked.connect(lambda: _clear_all())
        btn_layout.addWidget(clear_all_btn)

        btn_layout.addStretch()

        close_btn = AnimatedButton("关闭", button_type="primary")
        close_btn.clicked.connect(dialog.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

        def _delete_selected():
            item = tree.currentItem()
            if not item:
                return
            ip = item.text(0)
            profile_manager.delete_profile(ip)
            _refresh_profiles()
            self.status_bar.showMessage(f"已删除配置文件: {ip}")

        def _clear_all():
            reply = QMessageBox.question(dialog, "确认", "确定要清空所有配置文件吗？")
            if reply == QMessageBox.Yes:
                profile_manager.clear_all()
                _refresh_profiles()
                self.status_bar.showMessage("已清空所有配置文件")

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
            self.image_viewer.set_image(pixmap, rotation=0)  # PIL已旋转，不重复
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
        cache = self.cache_path
        try:
            if not cache or not os.path.exists(cache):
                self.info_label.setText("裁边失败: 缓存文件不存在")
                return
            with open(cache, "rb") as f:
                raw = f.read()
            img = Image.open(io.BytesIO(raw))
            cropped = auto_crop(img)
            if cropped is not img:
                buf = io.BytesIO()
                cropped = cropped.convert("RGB")
                cropped.save(buf, format="JPEG")
                with open(cache, "wb") as f:
                    f.write(buf.getvalue())
                self._cropped = True
                self._rotation = 0
                self._show_image()
        except Exception as e:
            self.info_label.setText(f"裁边失败: {type(e).__name__}")

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
                img.convert("RGB").save(pdf_path, "PDF", resolution=300.0)
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

        delete_btn = AnimatedButton("删除此页", button_type="secondary")
        delete_btn.clicked.connect(self._delete_current_page)
        btn_layout.addWidget(delete_btn)

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
            self.image_viewer.set_image(pixmap, rotation=0)  # PIL已旋转
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

    def _delete_current_page(self):
        """删除当前页"""
        if len(self.pages) <= 1:
            QMessageBox.warning(self, "提示", "至少需要保留一页")
            return

        page_num = self.pages[self.selected_idx]
        reply = QMessageBox.question(
            self, "确认", f"确定要删除第 {self.selected_idx + 1} 页吗？")
        if reply != QMessageBox.Yes:
            return

        # 删除缓存文件
        try:
            cache_path = self._cache_path(page_num)
            if os.path.exists(cache_path):
                os.remove(cache_path)
        except Exception:
            pass

        # 从列表中移除
        self.pages.pop(self.selected_idx)
        self._rotations.pop(page_num, None)
        self._cropped.discard(page_num)

        # 调整选中索引
        if self.selected_idx >= len(self.pages):
            self.selected_idx = len(self.pages) - 1

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
        history = history_manager.list_records(limit=9999)
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
    # Qt 6 默认使用 PerMonitorV2 DPI 感知，无需手动设置
    # 提前设置环境变量可抑制 Qt 内部的 SetProcessDpiAwarenessContext 警告
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 加载样式表
    qss = load_stylesheet()
    if qss:
        app.setStyleSheet(qss)

    load_embedded_font()

    window = ScanApp()
    # 不立即 show()，由启动加载界面在探测完成后自动显示
    # window.show()  # 已移至 _on_startup_complete()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
