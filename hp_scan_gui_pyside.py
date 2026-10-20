"""
惠普集成扫描工具 v4.0 — PySide6 重写版（UI 优化）
设计系统: Soft UI Evolution + Micro-interactions
功能: 磁盘缓存 / 并发扫描 / 多页ADF / WSD发现 / CLI / 双面扫描 / 页面旋转 / 自动裁边 / 空白页检测 / 监听模式 / 多目标输出 / 配置文件
"""

import io
import logging
import os
import sys
import threading
from datetime import datetime
from typing import Optional

from PIL import Image

import cache_manager
import history_manager
import page_meta
import preset_manager
import profile_manager
import scan_coordinator
import save_manager
from exposure import apply_exposure
from scan_coordinator import (
    ScanCoordinator, ScannerInfo, save_config,
    _label_for, _ip_label_for, DEFAULT_OUT_DIR,
    COLOR_MODE_MAP,
)
from escl_engine import auto_crop

# ────────── 全局异常日志 ──────────
def setup_exception_logging():
    """设置全局异常日志，捕获未处理异常写入文件"""
    import traceback
    log_file = os.path.join(os.path.expanduser("~"), "Desktop", "hp_scan_debug.log")

    def excepthook(exc_type, exc_value, exc_tb):
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*50}\n")
            f.write(f"[{datetime.now()}] Unhandled Exception:\n")
            f.write("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
            f.write(f"{'='*50}\n")
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook

    # 同时配置logging写入文件
    logging.basicConfig(
        filename=log_file,
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        encoding="utf-8",
    )

setup_exception_logging()
logger = logging.getLogger(__name__)
logger.info("=" * 50)
logger.info("程序启动")
logger.info(f"frozen={getattr(sys, 'frozen', False)}")
logger.info(f"executable={sys.executable}")
logger.info(f"cwd={os.getcwd()}")

# ────────── PySide6 导入 ──────────
from PySide6.QtCore import (
    Qt, Signal, QTimer, QSize,
    QPropertyAnimation, QEasingCurve,
    QRect, QObject,
)
from PySide6.QtGui import (
    QPixmap, QImage, QIcon, QFont, QFontDatabase,
    QPainter, QColor, QTransform,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QFrame,
    QComboBox, QLineEdit, QCheckBox, QProgressBar, QScrollArea,
    QFileDialog, QMessageBox, QDialog,
    QGroupBox, QSpinBox,
    QSizePolicy, QFrame, QStatusBar,
    QTreeWidget, QTreeWidgetItem,
    QSplitter, QToolButton,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect,
    QSlider, QInputDialog,
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
    """获取资源路径（PyInstaller 兼容）
    
    开发模式：从 resources/ 目录加载
    打包模式：从 sys._MEIPASS 加载（PyInstaller 临时目录）
    """
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "resources", relative_path)


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


# ────────── 跨线程 UI 投递 ──────────
class _UiPostMixin:
    """
    跨线程 UI 投递：工作线程 → 主线程。

    QTimer.singleShot 在工作线程里不会触发（批次 3 已修的 Critical），
    统一走 Signal(object) 队列连接：worker 线程 emit → 主线程槽里执行。

    ScanApp 与两个预览对话框共用这一份实现。
    """
    _ui_post = Signal(object)

    def _run_ui_post(self, fn):
        """在主线程执行投递过来的可调用对象"""
        fn()

    def _post_ui(self, fn):
        """从任意线程安全地把 fn 投递到主线程执行"""
        self._ui_post.emit(fn)


# ────────── 图片落盘 / 裁边 / 保存（GUI 层共用） ──────────
def _ext_format(ext):
    """扩展名 → PIL 格式名。未知扩展名退回 PNG（无损、不撒谎）。"""
    e = (ext or "").lower().lstrip(".")
    if e in ("jpg", "jpeg"):
        return "JPEG"
    if e == "png":
        return "PNG"
    if e in ("tif", "tiff"):
        return "TIFF"
    if e == "bmp":
        return "BMP"
    if e == "webp":
        return "WEBP"
    return "PNG"


def _encode_for_format(img, fmt):
    """
    按目标格式挑一个**能存**的模式，尽量不丢信息。

    - PNG/TIFF/BMP/WEBP：原样保存（alpha、调色板、灰度都保住）
    - JPEG：只支持 L / RGB / CMYK ⇒ 其余模式（RGBA、P、LA…）才转 RGB
    """
    if fmt == "JPEG":
        if img.mode in ("L", "RGB", "CMYK"):
            return img
        return img.convert("RGB")
    return img


def _write_image_atomic(img, path, ext):
    """
    原子写一张图：先写同目录临时文件，再 os.replace 覆盖目标。

    - 格式由**扩展名**决定（.png ⇒ PNG，.jpg ⇒ JPEG），不再一律 JPEG
    - JPEG 固定 quality=95（与多页保存路径一致）
    - 中途异常不会毁掉已存在的目标文件（非原子覆盖的老问题）
    """
    fmt = _ext_format(ext)
    out = _encode_for_format(img, fmt)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        if fmt == "JPEG":
            out.save(tmp, "JPEG", quality=95)
        else:
            out.save(tmp, fmt)
        os.replace(tmp, path)
    finally:
        if out is not img:
            out.close()
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _detect_crop_rect(img, threshold=240, padding=5):
    """
    算出 `escl_engine.auto_crop` 实际使用的裁剪框。

    auto_crop 只返回图像、不返回框，而 page_meta 需要 crop_rect。
    这里的算法与 auto_crop 同源：把「灰度 < threshold」看作有内容，
    取有内容像素的最小外接框，再按同样的 padding 外扩。
    返回 (left, top, right, bottom)（right/bottom 为**开区间**），无内容时返回 None。
    """
    try:
        gray = img.convert("L")
        mask = gray.point(lambda v: 255 if v < threshold else 0, mode="L")
        bbox = mask.getbbox()
    except Exception:
        return None
    if not bbox:
        return None
    left, top, right, bottom = bbox          # right/bottom 开区间
    width, height = img.size
    return (
        max(0, left - padding),
        max(0, top - padding),
        min(width, right + padding),
        min(height, bottom + padding),
    )


def _save_page_specs(job_id, ext, output_path, page_specs, groups, **kwargs):
    """
    统一保存入口 —— 直接保存与预览保存**必须走同一个函数**，否则文件名会分叉。

    命名规则（与 review F-2 的建议一致）：
      - 单页单组 ⇒ `save_single` ⇒ `{base}.{ext}`（不加序号，与改造前一致）
      - 多页 / 多组 ⇒ `save_pages` ⇒ `{base}_001.{ext}`… / `{base}_{组号}_{组内号}.{ext}`

    返回 **SaveResult**（list 子类）：额外带 `.failed_pages`（失败页数），
    GUI 靠它把"部分页没存下来"摆到用户面前。
    """
    if len(page_specs) == 1 and len(groups) == 1:
        cache_file, rotation = page_specs[0]
        path = save_manager.save_single(
            cache_file, ext, output_path, rotation=rotation, job_id=job_id, **kwargs)
        return save_manager.SaveResult([path], failed_pages=0, saved_page_count=1)
    return save_manager.save_pages(
        job_id, ext, output_path, page_specs, groups, **kwargs)


def _format_save_summary(saved_files, total_pages, failed_pages,
                         warnings=None):
    """
    拼"保存完成"提示文案。

    必须同时说清三件事：存到哪、存了几个文件、有没有页失败。
    （此前只报第 1 个路径 ⇒ 用户以为只存了 1 个）
    """
    saved_files = list(saved_files or [])
    count = len(saved_files)
    lines = []
    if count == 1:
        lines.append(f"已保存到: {saved_files[0]}")
    else:
        directory = os.path.dirname(saved_files[0]) if saved_files else ""
        lines.append(f"已保存到: {directory}")
        lines.append(f"共 {count} 个文件:")
        for f in saved_files[:10]:
            lines.append(f"  {f}")
        if count > 10:
            lines.append(f"  …… 其余 {count - 10} 个见输出目录")
    if count > 1:
        lines.append(f"（共 {count} 个文件）")
    if failed_pages > 0:
        lines.append("")
        lines.append(f"⚠ 部分页面保存失败（{failed_pages}/{total_pages} 页），详见日志。")
    for w in (warnings or []):
        lines.append("")
        lines.append(f"⚠ {w}")
    return "\n".join(lines)


def _release_job_protection(job_id):
    """
    解除 job 的缓存保护（幂等）。

    落盘成功 → save_manager.remove_job 已经清过；
    用户放弃（关闭预览且不保存）→ 这里清，否则 job 永远留在受保护集合里，
    它的缓存目录永远不被 eviction 回收 ⇒ 缓存上限机制被架空。

    监听 / WIA 路径用的是 register_job，协调器路径用的是 register_pending_job，
    两个集合都要清。
    """
    if not job_id:
        return
    try:
        cache_manager.unregister_job(job_id)
        cache_manager.unregister_pending_job(job_id)
    except Exception as e:
        logger.debug("注销 job 失败: %s", e)


class _UiDispatcher(QObject):
    """
    模块级 UI 投递器：后台线程 → 主线程。

    为什么不用对话框自己的 `_ui_post`：后台保存还在跑的时候，对话框可能已经被
    销毁（用户关窗 / 测试里 deleteLater）⇒ 往已销毁的 QObject 上 emit 会踩到
    use-after-free，进程直接 heap corruption。这个对象是模块级的，永远不死。
    """

    _post = Signal(object)

    def __init__(self):
        super().__init__()
        self._post.connect(self._run)

    def _run(self, fn):
        try:
            fn()
        except RuntimeError as e:
            # 目标控件已被销毁（正常情况：用户提前关窗）
            logger.debug("UI 回调目标已销毁: %s", e)

    def post(self, fn):
        self._post.emit(fn)


_UI_DISPATCHER = None


def _ensure_ui_dispatcher():
    """
    取（并在主线程首次创建）模块级投递器。

    ★ QObject 的线程亲和性 = 创建它的线程 ⇒ 必须在**主线程**创建。
      万一第一次调用发生在工作线程，就把它 moveToThread 主线程
      （signal 的槽在接收端所在线程执行 ⇒ 仍是主线程）。
    """
    global _UI_DISPATCHER
    if _UI_DISPATCHER is None:
        _UI_DISPATCHER = _UiDispatcher()
        app = QApplication.instance()
        if app is not None and _UI_DISPATCHER.thread() is not app.thread():
            try:
                _UI_DISPATCHER.moveToThread(app.thread())
            except Exception as e:
                logger.debug("投递器移入主线程失败: %s", e)
    return _UI_DISPATCHER


def _post_to_ui(fn):
    """从任意线程把 fn 投递到主线程执行（模块级，不随对话框销毁）。"""
    _ensure_ui_dispatcher().post(fn)


def _run_in_background(work, on_done, on_error):
    """
    把 work 丢到后台线程跑；完成/失败都回**主线程**回调。

    用普通的 threading.Thread（和本文件里曝光重算的工作线程同一套写法），
    不用 QThreadPool/QRunnable：autoDelete 的 QRunnable 被 Qt 回收后，
    Python 包装对象再被 GC 会二次释放 ⇒ 进程 heap corruption。
    """
    def _runner():
        try:
            result = work()
        except Exception as exc:                   # noqa: BLE001 - 交给主线程提示
            # ★ 必须先赋给另一个名字：except 的 `as e` 在 except 块结束时会被
            #   删除，lambda 捕获到的是那个已删除的名字 ⇒ NameError
            err = exc
            logger.error("后台保存失败: %s", err, exc_info=True)
            _post_to_ui(lambda: on_error(err))
        else:
            _post_to_ui(lambda: on_done(result))

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return thread


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


# ────────── 缓存配置契约（GUI ↔ cache_manager）──────────
# 缓存设置对话框读写的键名必须与 cache_manager._DEFAULTS 完全一致。
# 历史缺陷：GUI 侧写的是 "limit_mb"，而 cache_manager 真实键是 "cache_limit_mb"
# —— 用户改了上限保存后完全没效果。tests/test_gui_consistency.py 会断言两者一致。
CACHE_SETTING_KEYS = ("cache_limit_mb", "trigger_ratio", "cleanup_mb", "stale_hours")

# 历史版本误用过的键名：保存时一并清掉，避免残留在配置文件里误导排查
_LEGACY_CACHE_KEYS = ("limit_mb",)


def apply_cache_settings(config: dict, limit_mb: int, trigger_percent: int,
                         cleanup_mb: int) -> dict:
    """
    把「缓存设置」对话框上的值写入缓存配置字典（原地修改并返回）。

    键名严格取自 cache_manager._DEFAULTS —— GUI 与 cache_manager 之间只靠这一份
    键名契约对齐，改名必须两边同时改。stale_hours 对话框不暴露，属于同一份配置
    契约，这里做缺省补齐，保证写出的键集合与 _DEFAULTS 一致。
    """
    config["cache_limit_mb"] = int(limit_mb)
    config["trigger_ratio"] = float(trigger_percent) / 100.0
    config["cleanup_mb"] = int(cleanup_mb)
    config.setdefault("stale_hours", cache_manager._DEFAULTS["stale_hours"])
    for legacy in _LEGACY_CACHE_KEYS:
        config.pop(legacy, None)
    return config


# ────────── 主窗口 ──────────
class ScanApp(QMainWindow, _UiPostMixin):
    """主窗口（UI 优化版）"""

    # 跨线程信号：发现完成
    discover_finished = Signal()

    # 预览里设置的曝光参数 —— 直接保存模式沿用（跳过预览时没有别的来源）
    _exposure_state = None

    def __init__(self):
        super().__init__()

        # 业务逻辑层（快速）
        self.coordinator = ScanCoordinator()
        self.coordinator.scan_progress.connect(self._on_scan_progress)
        self.coordinator.scan_finished.connect(self._on_scan_finished)
        self.coordinator.scan_failed.connect(self._on_scan_failed)
        self.coordinator.scan_cancelled.connect(self._on_scan_cancelled)
        # F-3：协调器因为「设备能力未确认」而按用户显式选择走时给的用户可见提示
        # （不接 ⇒ 用户不知道"你选的输稿器可能用不了"）
        try:
            self.coordinator.source_warning.connect(self._on_source_warning)
        except Exception as e:            # pragma: no cover - 旧版协调器没这个信号
            logger.debug("协调器没有 source_warning 信号: %s", e)
        _ensure_ui_dispatcher()
        self._current_job_id = None
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
        # 预览里设置的曝光参数 —— 直接保存模式沿用（跳过预览时没有别的来源）
        self._exposure_state = None

        # 先构建界面（隐藏状态）
        self._setup_ui()
        self._refresh_extra_dirs()

        # 连接跨线程信号
        self.discover_finished.connect(self._on_discover_finished)
        self._ui_post.connect(self._run_ui_post)

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
        icon_path = get_resource_path(os.path.join("icons", "icon_64.png"))
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
        """启动流程：立即显示已保存设备 → 后台并行探测能力"""
        logger.info("_do_startup_scan 被调用")
        # 抑制 SSL 警告
        try:
            import urllib3; urllib3.disable_warnings()
        except Exception:
            pass
        try:
            import warnings; warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        except Exception:
            pass

        # 第1步：恢复已保存的扫描仪并立即显示
        saved = self.coordinator.restore_saved_scanners()
        logger.info(f"恢复已保存扫描仪: {len(saved)} 台")
        self._rebuild_cards()
        QTimer.singleShot(0, self._finish_loading)
        if not saved:
            logger.info("无已保存扫描仪，直接进入主界面")
            return

        # 第2步：后台并行探测设备能力（不阻塞UI）
        self._probe_state = {
            "current": 0, "total": len(saved),
            "detail": "", "done": False, "errors": [],
        }

        # 主线程轮询器：每 200ms 更新状态栏
        def _poll():
            s = self._probe_state
            if s["done"]:
                self._poll_timer.stop()
                self._rebuild_cards()  # 用探测到的能力更新卡片
                # 自动选中第一台
                if self.coordinator.scanners and self.selected_idx < 0:
                    self._on_card_clicked(0)
                count = len(self.coordinator.scanners)
                self.status_bar.showMessage(f"就绪 — {count} 台扫描仪")
                return
            self.status_bar.showMessage(
                f"正在探测 {s['detail']} ({s['current']}/{s['total']})")

        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(_poll)
        self._poll_timer.start(200)

        # 看门狗：20s 后强制结束
        QTimer.singleShot(20000, self._on_probe_watchdog)

        # 后台线程：并行探测所有设备
        def _probe_thread():
            from escl_engine import fetch_capabilities, probe_escl
            import concurrent.futures
            def _probe_one(idx, scanner):
                self._probe_state["current"] = idx
                self._probe_state["detail"] = scanner.display_name
                try:
                    if not scanner.escl_url and scanner.ip:
                        url = probe_escl(scanner.ip, timeout=3.0)
                        if url:
                            scanner.escl_url = url
                    if scanner.escl_url:
                        fetch_capabilities(scanner, timeout=3.0)
                except Exception as e:
                    self._probe_state["errors"].append((scanner.display_name, str(e)))
                self._probe_state["current"] = idx + 1

            # 并行探测（最多4个并发）
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(_probe_one, idx, s)
                    for idx, s in enumerate(saved)
                ]
                concurrent.futures.wait(futures, timeout=20)
            self._probe_state["done"] = True

        threading.Thread(target=_probe_thread, daemon=True).start()

    def _on_probe_watchdog(self):
        """看门狗触发：强制结束等待"""
        if hasattr(self, '_probe_state') and not self._probe_state.get("done"):
            self._probe_state["done"] = True

    def _finish_loading(self):
        """无保存设备时：直接进入主界面，让用户点刷新"""
        if self._loading:
            self._loading.close()
            self._loading = None
        self.show()
        self.status_bar.showMessage("点击「刷新」搜索打印机，或「手动添加」输入IP")

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
        self.resolution_combo = self._create_styled_combo(["150", "200", "300", "600"])
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
        self._sync_direct_save_checkbox()
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

    def _sync_direct_save_checkbox(self):
        """
        复选框显示状态必须与实际生效状态一致。

        direct_save_val 来自配置（上次退出时的勾选状态），而复选框默认未勾选
        —— 不对齐的话会出现"框是灰的、实际却在直接保存"的错觉。屏蔽信号避免
        启动时多写一次配置文件。
        """
        self.direct_save_check.blockSignals(True)
        self.direct_save_check.setChecked(bool(self.direct_save_val))
        self.direct_save_check.blockSignals(False)

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
        logger.info("_auto_discover 被调用")
        # 防止重复点击
        if hasattr(self, '_discovering') and self._discovering:
            logger.warning("_auto_discover 已在运行，跳过")
            return
        self._discovering = True

        self.status_bar.showMessage("正在发现扫描仪...")
        logger.info("开始发现扫描仪...")

        def _on_complete(new_count, new_scanners):
            logger.info(f"发现完成: {new_count} 台新设备")
            self._discovering = False
            self.discover_finished.emit()

        self.coordinator.discover_scanners_background(_on_complete)

    def _on_discover_finished(self):
        """发现完成更新 UI"""
        logger.info("_on_discover_finished 被调用")
        self._rebuild_cards()
        count = len(self.coordinator.scanners)
        logger.info(f"_rebuild_cards 完成: {count} 台扫描仪")
        self.status_bar.showMessage(f"发现 {count} 台扫描仪")

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
        """开始扫描（来源决策与执行下沉到协调器）"""
        if not self.selected:
            QMessageBox.warning(self, "提示", "请先选择一台打印机")
            return

        # 每次扫描都是新文档 ⇒ 清掉上一份文档残留的曝光参数（I-6）。
        # 曝光是「预览里的临时调整」，跨文档静默套用会让用户扫出来的图被改过。
        self._reset_exposure_state()

        out_dir = self.dir_entry.text() or self.output_dir
        try:
            out_dir = os.path.abspath(out_dir)
        except (ValueError, OSError):
            QMessageBox.critical(self, "错误", "保存路径无效")
            return
        if not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        self._save_profile()

        params = {
            "resolution": self.resolution_val,
            "color_mode_ui": self.color_mode_val,
            "output_format": self.output_format_val,
            "source": self.source_combo.currentText(),
            "duplex": self.duplex_val,
            "output_dir": out_dir,
        }

        self.scan_btn.setText("扫描中...")
        self.scan_btn.setEnabled(False)
        self.status_bar.showMessage("正在扫描...")
        self.scan_progress.setVisible(True)
        self.scan_progress.setRange(0, 0)

        self.cancel_scan_btn = AnimatedButton("取消扫描", button_type="accent")
        self.cancel_scan_btn.clicked.connect(self._cancel_scan)
        self.status_bar.addPermanentWidget(self.cancel_scan_btn)

        self._current_job_id = self.coordinator.start_scan(self.selected, params)

    def _cancel_scan(self):
        """取消扫描"""
        job_id = getattr(self, "_current_job_id", None)
        if job_id:
            self.coordinator.cancel_scan(job_id)
        self.status_bar.showMessage("扫描已取消")
        self._reset_scan_ui()

    # ────────── 协调器信号槽 ──────────

    def _on_scan_progress(self, job_id, message, page):
        self.status_bar.showMessage(message)

    def _on_source_warning(self, warning):
        """
        协调器的来源提示（能力未确认 / 来源被回退等）—— 必须让用户看见。

        信号签名：`source_warning = Signal(str)`（只有提示文本，没有 job_id）。
        """
        if not warning:
            return
        logger.info("来源提示: %s", warning)
        try:
            self.status_bar.showMessage(f"提示: {warning}")
            QMessageBox.warning(self, "扫描提示", warning)
        except RuntimeError:
            logger.debug("来源提示时主窗口已销毁")

    def _on_scan_finished(self, job_id, result):
        self._reset_scan_ui()
        if not result.cached_paths:
            self.status_bar.showMessage("扫描未产生任何页面")
            return
        scanner = self.selected
        # 「直接保存」勾选 ⇒ 跳过预览对话框，直接落盘
        if self.direct_save_val:
            self._direct_save_result(result)
            return
        # 扫描结束、结果交给用户预览之前：把 job 登记为「已扫描未保存」，
        # 让它的缓存目录在用户预览期间不受 eviction / stale 清理影响。
        # 注销时机：落盘成功（save_manager.remove_job）或预览关闭未保存
        # （PreviewDialog/MultiPagePreviewDialog._release_job）。
        try:
            cache_manager.register_pending_job(result.job_id)
        except Exception as e:
            logger.debug("登记待保存 job 失败: %s", e)
        if result.page_count > 1:
            self._show_multi_preview(result.job_id, result.ext, result.output_path,
                                    result.page_count, scanner)
        else:
            self._show_preview(result.cached_paths[0], result.ext, result.output_path,
                              result.job_id, scanner)

    def _direct_save_result(self, result):
        """
        「直接保存」模式：跳过预览，直接把缓存页落盘。

        save_manager 负责：写目标文件 → 复制到额外输出目录 → 清缓存 → 写扫描历史。
        设备名/IP 取自当前选中设备；未选中设备时留空，不报错。

        曝光参数：只在**本次扫描内**沿用（扫描开始时已清空残留），
        提示里会写明"沿用了曝光"，避免用户不知道图被动过。
        """
        scanner = self.selected
        dev_name = (scanner.model or "") if scanner else ""
        dev_ip = (scanner.ip or "") if scanner else ""
        page_specs = [(path, 0) for path in result.cached_paths]
        groups = [list(range(len(page_specs)))]
        # getattr：ScanApp 的最小替身（只借用方法）可能没有这个属性。
        exposure = getattr(self, "_exposure_state", None)
        total_pages = len(page_specs)
        try:
            saved_files = _save_page_specs(
                result.job_id, result.ext, result.output_path, page_specs, groups,
                extra_dirs=self.extra_dirs or [], device_name=dev_name,
                device_ip=dev_ip, exposure=exposure)
        except Exception as e:
            logger.error("直接保存失败: %s", e, exc_info=True)
            QMessageBox.critical(self, "保存失败", f"直接保存失败: {e}")
            self.status_bar.showMessage("直接保存失败")
            _release_job_protection(result.job_id)
            return
        if not saved_files:
            self.status_bar.showMessage("直接保存未产生文件")
            _release_job_protection(result.job_id)
            return
        # 部分页保存失败：save_manager 现在把失败页数放进 SaveResult.failed_pages
        # （此前只写日志 ⇒ 用户拿到少了页的文件却不知道）
        failed_pages = int(getattr(saved_files, "failed_pages", 0) or 0)
        warnings = list(getattr(saved_files, "warnings", []) or [])
        summary = _format_save_summary(saved_files, total_pages, failed_pages,
                                       warnings)
        if exposure:
            summary += f"\n（沿用曝光参数：{exposure.get('mode')}）"
        if failed_pages > 0:
            self.status_bar.showMessage(
                f"已保存 {len(saved_files)} 个文件，但有 {failed_pages} 页保存失败")
        else:
            self.status_bar.showMessage(f"已保存 {len(saved_files)} 个文件")
        QMessageBox.information(
            self, "保存完成" if not failed_pages else "保存部分失败", summary)

    def _release_job(self, job_id):
        """解除 job 的缓存保护（幂等）—— 见模块级 _release_job_protection。"""
        _release_job_protection(job_id)


    def _on_scan_failed(self, job_id, error):
        self._reset_scan_ui()
        QMessageBox.critical(self, "扫描错误", error)
        self.status_bar.showMessage(f"扫描失败: {error[:50]}")

    def _on_scan_cancelled(self, job_id):
        self._reset_scan_ui()
        self.status_bar.showMessage("扫描已取消")

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
    def set_exposure_state(self, exposure):
        """
        记住预览里设置的曝光参数。

        这是**本次扫描内**的临时状态：每次开始新扫描都会被 `_reset_exposure_state`
        清掉 ⇒ 不会把上一份文档的曝光静默套到下一份文档上（I-6）。
        """
        self._exposure_state = dict(exposure) if exposure else None

    def _reset_exposure_state(self):
        """清掉残留的曝光参数（新扫描开始时调用）"""
        self._exposure_state = None

    def _show_preview(self, cache_path, ext, output_path, job_id, scanner=None):
        """显示单页预览"""
        self._reset_scan_ui()
        dev_name = (scanner.model or "") if scanner else ""
        dev_ip = scanner.ip if scanner else ""
        exposure = getattr(self, "_exposure_state", None)
        dialog = PreviewDialog(self, cache_path, ext, output_path, job_id,
                               device_name=dev_name, device_ip=dev_ip,
                               extra_dirs=self.extra_dirs,
                               exposure=exposure)
        dialog.exec()

    def _show_multi_preview(self, job_id, ext, output_path, page_count, scanner=None):
        """显示多页预览"""
        self._reset_scan_ui()
        dev_name = (scanner.model or "") if scanner else ""
        dev_ip = scanner.ip if scanner else ""
        exposure = getattr(self, "_exposure_state", None)
        dialog = MultiPagePreviewDialog(self, job_id, ext, output_path, page_count,
                                        device_name=dev_name, device_ip=dev_ip,
                                        extra_dirs=self.extra_dirs,
                                        exposure=exposure)
        dialog.exec()

    # ────────── 监听模式 ──────────
    def _start_listen(self):
        """启动监听模式"""
        if not self.selected:
            QMessageBox.warning(self, "提示", "请先选择一台打印机")
            return

        from listen_engine import listen_for_scan, listen_for_multipage_scan

        # 新的一次监听 = 新文档 ⇒ 清掉残留曝光（I-6）
        self._reset_exposure_state()

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
            use_adf = False  # 监听模式不自动探测 ADF
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
            self._post_ui(lambda: self.status_bar.showMessage(f"监听模式：{msg}"))

        def _listen_thread():
            job_id = None
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
                        self._post_ui(lambda: self._show_multi_preview(
                            job_id, ext, output_path, len(pages), scanner))
                    else:
                        self._post_ui(lambda: self.status_bar.showMessage("监听模式：超时或已取消"))
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
                        self._post_ui(lambda: self._show_preview(
                            cache_path, ext, output_path, job_id, scanner))
                    else:
                        self._post_ui(lambda: self.status_bar.showMessage("监听模式：超时或已取消"))
            except Exception as e:
                self._post_ui(lambda: self._scan_error(f"监听模式错误: {e}"))
                # 预览没能开起来 ⇒ 没人替我们注销 job，必须在这里清掉，
                # 否则它永远留在受保护集合里，缓存目录永远不被 eviction 回收。
                self._release_job(job_id)
            finally:
                self._post_ui(self._reset_scan_ui)

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
                    self._post_ui(lambda: status_label.setText(
                        f"✓ 发现 eSCL 服务: {url}"))
                    self._post_ui(lambda: add_btn.setEnabled(True))
                else:
                    self._post_ui(lambda: status_label.setText(
                        "✗ 未发现 eSCL 服务"))
                self._post_ui(lambda: probe_btn.setEnabled(True))

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
            self._post_ui(lambda: self._on_status_received(status))

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
            from wia_engine import list_wia_scanners, wia_scan
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
            combo.addItem(s.get("name", s.get("id", "未知")))
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
            # 新的一次扫描 = 新文档 ⇒ 清掉残留曝光（I-6）
            self._reset_exposure_state()

            def _wia_thread():
                job_id = None
                try:
                    data, ext = wia_scan(
                        device_id=device.get("id"),
                        resolution=self.resolution_val,
                        color_mode=1,
                        output_format=self.output_format_val,
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
                        self._post_ui(lambda: self._show_preview(
                            cache_path, ext, output_path, job_id))
                    else:
                        self._post_ui(lambda: self.status_bar.showMessage("WIA 扫描失败"))
                except Exception as e:
                    self._post_ui(lambda: self._scan_error(f"WIA 错误: {e}"))
                    # 预览没开起来 ⇒ 没人替我们注销 job（否则缓存目录永不被回收）
                    self._release_job(job_id)

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
        limit_spin.setValue(config.get("cache_limit_mb", cache_manager._DEFAULTS["cache_limit_mb"]))
        limit_layout.addWidget(limit_spin)
        layout.addLayout(limit_layout)

        # 触发比例
        trigger_layout = QHBoxLayout()
        trigger_layout.addWidget(QLabel("触发比例 (%):"))
        trigger_spin = QSpinBox()
        trigger_spin.setRange(50, 99)
        trigger_spin.setValue(int(config.get("trigger_ratio", cache_manager._DEFAULTS["trigger_ratio"]) * 100))
        trigger_layout.addWidget(trigger_spin)
        layout.addLayout(trigger_layout)

        # 清理大小
        cleanup_layout = QHBoxLayout()
        cleanup_layout.addWidget(QLabel("清理大小 (MB):"))
        cleanup_spin = QSpinBox()
        cleanup_spin.setRange(50, 500)
        cleanup_spin.setValue(config.get("cleanup_mb", cache_manager._DEFAULTS["cleanup_mb"]))
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
            apply_cache_settings(config, limit_spin.value(), trigger_spin.value(),
                                 cleanup_spin.value())
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
        try:
            self.coordinator.shutdown()
        except Exception:
            pass
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


# ────────── 曝光工具条 ──────────
class ExposurePanel(QWidget):
    """
    曝光工具条 —— 预览时实时调参。

    面板只管「参数状态 + 控件」，不管图像怎么渲染：
    参数变化时发 params_changed，由预览对话框负责重算并刷新 QPixmap。
    工具条可折叠，默认收起（不挤占预览空间）。
    """

    params_changed = Signal()      # 参数变化 → 需要重算预览
    mode_changed = Signal(str)     # 模式变化（"off" / "auto" / "manual"）

    MODE_OFF = "off"
    MODE_AUTO = "auto"
    MODE_MANUAL = "manual"

    DEBOUNCE_MS = 200              # 拖滑块防抖：连续拖动不连续重算

    # key, 标签, 滑块最小, 滑块最大, 默认, 缩放系数（滑块值 × 缩放 = 参数值）
    # gamma 的参数域是 0.1~3.0 ⇒ 滑块 10~300、缩放 0.01（默认 100 ⇒ 1.00）
    _SLIDER_SPECS = (
        ("brightness", "明暗", -100, 100, 0, 1),
        ("contrast", "对比度", -100, 100, 0, 1),
        ("gamma", "伽马", 10, 300, 100, 0.01),
        ("shadows", "阴影", -100, 100, 0, 1),
        ("highlights", "高光", -100, 100, 0, 1),
        ("r_gain", "R", 50, 150, 100, 0.01),
        ("g_gain", "G", 50, 150, 100, 0.01),
        ("b_gain", "B", 50, 150, 100, 0.01),
    )

    def __init__(self, parent=None, exposure=None):
        super().__init__(parent)
        self._mode = self.MODE_OFF
        self._params = {
            "brightness": 0, "contrast": 0, "gamma": 1.0,
            "shadows": 0, "highlights": 0,
            "channel_gains": [1.0, 1.0, 1.0],
        }
        self._suppress = False
        self._sliders = {}        # key -> (QSlider, 缩放系数)
        self._value_labels = {}   # key -> QLabel

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_params_changed)

        self._setup_ui()
        self.set_exposure(exposure)
        self.reload_presets()

    # ────────── 构建 ──────────
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACING["sm"])

        # 折叠头（默认收起）
        self.header_btn = QToolButton()
        self.header_btn.setText("曝光")
        self.header_btn.setCheckable(True)
        self.header_btn.setChecked(False)
        self.header_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header_btn.setArrowType(Qt.RightArrow)
        self.header_btn.setStyleSheet(
            f"QToolButton {{ border: none; color: {COLORS['text_secondary']}; "
            f"font-weight: 600; }}")
        self.header_btn.toggled.connect(self._on_header_toggled)
        root.addWidget(self.header_btn)

        # 折叠体
        self.body = QFrame()
        self.body.setStyleSheet(
            f"QFrame {{ background: {COLORS['surface']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: {RADIUS['md']}px; }}")
        body = QVBoxLayout(self.body)
        body.setContentsMargins(SPACING["sm"], SPACING["sm"],
                               SPACING["sm"], SPACING["sm"])
        body.setSpacing(SPACING["sm"])

        # 第 1 行：模式 + 预设 + 保存/删除
        row = QHBoxLayout()
        row.setSpacing(SPACING["sm"])

        row.addWidget(QLabel("模式"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("关闭", self.MODE_OFF)
        self.mode_combo.addItem("自动（去灰底）", self.MODE_AUTO)
        self.mode_combo.addItem("手动", self.MODE_MANUAL)
        self.mode_combo.setFixedWidth(130)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        row.addWidget(self.mode_combo)

        row.addWidget(QLabel("预设"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(130)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        row.addWidget(self.preset_combo)

        self.save_preset_btn = QPushButton("保存当前为预设")
        self.save_preset_btn.clicked.connect(self._save_current_preset)
        row.addWidget(self.save_preset_btn)

        self.delete_preset_btn = QPushButton("删除预设")
        self.delete_preset_btn.clicked.connect(self._delete_current_preset)
        row.addWidget(self.delete_preset_btn)

        row.addStretch()
        body.addLayout(row)

        # 第 2 行起：手动参数滑块
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACING["md"])
        grid.setVerticalSpacing(SPACING["xs"])
        for idx, spec in enumerate(self._SLIDER_SPECS):
            grid.addWidget(self._make_slider(*spec), idx // 3, idx % 3)
        grid.setColumnStretch(3, 1)
        body.addLayout(grid)

        root.addWidget(self.body)
        self.body.setVisible(False)

    def _make_slider(self, key, label, lo, hi, default, scale):
        """一个带标签和数值显示的滑块。返回容器 widget。"""
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SPACING["xs"])

        lbl = QLabel(label)
        lbl.setFixedWidth(36)
        lay.addWidget(lbl)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(default)
        slider.setMinimumWidth(90)
        slider.valueChanged.connect(self._on_slider_changed)
        lay.addWidget(slider, 1)

        val = QLabel("")
        val.setFixedWidth(44)
        val.setStyleSheet(f"color: {COLORS['text_secondary']};")
        lay.addWidget(val)

        self._sliders[key] = (slider, scale)
        self._value_labels[key] = val
        return box

    # ────────── 状态读写 ──────────
    def exposure_dict(self):
        """
        当前曝光参数。关闭时返回 None（save_manager 收到 None ⇒ 与改造前完全一致）。

        channel_gains 统一成 tuple —— 列表与元组比较不相等，
        不规范化会让 apply_exposure 误判「通道增益有改动」。
        """
        if self._mode == self.MODE_OFF:
            return None
        gains = tuple(round(float(g), 3) for g in self._params["channel_gains"])
        return {
            "mode": self._mode,
            "brightness": int(self._params["brightness"]),
            "contrast": int(self._params["contrast"]),
            "gamma": float(self._params["gamma"]),
            "shadows": int(self._params["shadows"]),
            "highlights": int(self._params["highlights"]),
            "channel_gains": gains,
        }

    def set_exposure(self, exposure):
        """回填曝光参数（不触发重算 —— 回填后由调用方决定要不要重算）。"""
        prev = self._suppress
        self._suppress = True
        try:
            if not exposure:
                self._mode = self.MODE_OFF
                self._params = {
                    "brightness": 0, "contrast": 0, "gamma": 1.0,
                    "shadows": 0, "highlights": 0,
                    "channel_gains": [1.0, 1.0, 1.0],
                }
            else:
                self._mode = str(exposure.get("mode") or self.MODE_OFF)
                gains = list(exposure.get("channel_gains") or (1.0, 1.0, 1.0))
                if len(gains) != 3:
                    gains = [1.0, 1.0, 1.0]
                self._params = {
                    "brightness": int(exposure.get("brightness") or 0),
                    "contrast": int(exposure.get("contrast") or 0),
                    "gamma": float(exposure.get("gamma") or 1.0),
                    "shadows": int(exposure.get("shadows") or 0),
                    "highlights": int(exposure.get("highlights") or 0),
                    "channel_gains": [float(g) for g in gains],
                }

            idx = self.mode_combo.findData(self._mode)
            if idx >= 0:
                self.mode_combo.setCurrentIndex(idx)
            self._sync_sliders()
            self._update_value_labels()
            self._update_manual_enabled()
        finally:
            self._suppress = prev

    def _sync_sliders(self):
        """把参数状态写回滑块。"""
        prev = self._suppress
        self._suppress = True
        try:
            for key, (slider, scale) in self._sliders.items():
                if key == "r_gain":
                    value = self._params["channel_gains"][0]
                elif key == "g_gain":
                    value = self._params["channel_gains"][1]
                elif key == "b_gain":
                    value = self._params["channel_gains"][2]
                else:
                    value = self._params[key]
                slider.setValue(int(round(value / scale)))
        finally:
            self._suppress = prev

    def _read_sliders(self):
        """把滑块当前值写回参数状态。"""
        for key, (slider, scale) in self._sliders.items():
            value = slider.value() * scale
            if key == "r_gain":
                self._params["channel_gains"][0] = round(value, 3)
            elif key == "g_gain":
                self._params["channel_gains"][1] = round(value, 3)
            elif key == "b_gain":
                self._params["channel_gains"][2] = round(value, 3)
            elif key == "gamma":
                self._params["gamma"] = round(value, 2)
            else:
                self._params[key] = int(round(value))

    def _update_value_labels(self):
        for key, (slider, scale) in self._sliders.items():
            value = slider.value() * scale
            if key in ("r_gain", "g_gain", "b_gain"):
                text = f"{value:.2f}"
            elif key == "gamma":
                text = f"{value:.2f}"
            else:
                text = f"{int(round(value)):+d}" if value else "0"
            self._value_labels[key].setText(text)

    def _update_manual_enabled(self):
        """只有「手动」模式才允许调参数。"""
        enabled = self._mode == self.MODE_MANUAL
        for slider, _scale in self._sliders.values():
            slider.setEnabled(enabled)

    # ────────── 交互 ──────────
    def _on_header_toggled(self, checked):
        self.body.setVisible(checked)
        self.header_btn.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _on_mode_changed(self, _index):
        if self._suppress:
            return
        self._mode = self.mode_combo.currentData() or self.MODE_OFF
        self._debounce.stop()
        self._update_manual_enabled()
        self.mode_changed.emit(self._mode)
        self.params_changed.emit()

    def _on_slider_changed(self, _value):
        if self._suppress:
            return
        self._read_sliders()
        self._update_value_labels()
        self._update_manual_enabled()
        # 拖滑块防抖：连续拖动时不连续重算
        self._debounce.start()

    def _emit_params_changed(self):
        self.params_changed.emit()

    # ────────── 预设 ──────────
    def reload_presets(self):
        """刷新预设下拉（内置 + 用户自定义）。"""
        current = self.preset_combo.currentData()
        prev = self._suppress
        self._suppress = True
        try:
            self.preset_combo.clear()
            self.preset_combo.addItem("（不使用预设）", None)
            for p in preset_manager.load_presets():
                self.preset_combo.addItem(p.get("name", ""), p.get("name"))
            idx = self.preset_combo.findData(current)
            self.preset_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._suppress = prev
        self._update_delete_enabled()

    def _find_preset(self, name):
        for p in preset_manager.load_presets():
            if p.get("name") == name:
                return p
        return None

    def _on_preset_selected(self, _index):
        if self._suppress:
            return
        name = self.preset_combo.currentData()
        if not name:
            return
        preset = self._find_preset(name)
        if not preset:
            return
        self.set_exposure(preset)          # 回填模式与全部参数
        self._update_delete_enabled()
        self.params_changed.emit()         # 套用后立即重算

    def _save_current_preset(self):
        name, ok = QInputDialog.getText(self, "保存预设", "预设名称：")
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            return
        preset = {
            "name": name,
            "mode": self._mode,
            "brightness": int(self._params["brightness"]),
            "contrast": int(self._params["contrast"]),
            "gamma": float(self._params["gamma"]),
            "shadows": int(self._params["shadows"]),
            "highlights": int(self._params["highlights"]),
            "channel_gains": [round(float(g), 3)
                              for g in self._params["channel_gains"]],
        }
        preset_manager.save_user_preset(preset)
        self.reload_presets()
        idx = self.preset_combo.findData(name)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)

    def _delete_current_preset(self):
        name = self.preset_combo.currentData()
        if not name:
            return
        preset = self._find_preset(name)
        if preset is None:
            return
        if preset.get("builtin"):
            QMessageBox.information(self, "提示", "内置预设不可删除")
            return
        preset_manager.delete_user_preset(name)
        self.reload_presets()

    def _update_delete_enabled(self):
        name = self.preset_combo.currentData()
        builtin = False
        if name:
            preset = self._find_preset(name)
            builtin = bool(preset and preset.get("builtin"))
        self.delete_preset_btn.setEnabled(bool(name) and not builtin)
        self.delete_preset_btn.setToolTip("内置预设不可删除" if builtin else "")


# ────────── 预览对话框 ──────────
class PreviewDialog(QDialog, _UiPostMixin):
    """单页预览对话框（UI 优化版）"""

    def __init__(self, parent, cache_path, ext, output_path, job_id,
                 device_name="", device_ip="", extra_dirs=None, exposure=None):
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
        # 裁边结果文件（不覆盖缓存原件）；裁边框（左上右下，右/下为开区间）
        self._crop_path = None
        self._crop_rect = None
        # 递增 token：auto 重算完成后用它判断结果是否已过期
        self._exposure_token = 0
        self._initial_exposure = exposure

        self.setWindowTitle("扫描预览")
        self.setMinimumSize(520, 420)
        self.resize(620, 520)

        # C++ 对象是否还活着（destroyed 信号是唯一可靠的判据：
        # shiboken6.isValid 对已 deleteLater 的对象仍返回 True）
        self._alive = True
        self.destroyed.connect(self._mark_destroyed)
        _ensure_ui_dispatcher()
        self._setup_ui()
        self._show_image()
        add_fade_animation(self, ANIM["normal"])

    # ────────── 线程 / 生命周期 ──────────
    def _post_ui(self, fn):
        """
        投递到主线程。

        ★ 走**模块级**投递器，不用本对话框自己的 `_ui_post` 信号：
          后台线程（曝光重算 / 后台保存）可能在对话框已被销毁之后才投递，
          往已销毁的 QObject 上 emit 会踩野指针 ⇒ 进程堆损坏（0xc0000374）。
        """
        _post_to_ui(fn)

    def _mark_destroyed(self, *args):
        """C++ 对象已析构 ⇒ 之后不能再碰它的任何控件。"""
        self._alive = False

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

        # 曝光工具条（可折叠，默认收起）
        self.exposure_panel = ExposurePanel(self, exposure=self._initial_exposure)
        self.exposure_panel.params_changed.connect(self._on_exposure_changed)
        layout.addWidget(self.exposure_panel)

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

    # ────────── 曝光 ──────────
    def _on_exposure_changed(self):
        """曝光参数变化 → 重算预览。"""
        self._recompute_preview()

    def _recompute_preview(self):
        """
        按当前曝光参数重算预览。

        性能（300dpi A4 实测，主线程）：manual 约 **0.130s/页**
        （decode 0.026s + apply_exposure 0.065s + QPixmap 0.039s）
        ⇒ 手动模式仍在主线程同步算（量级可接受，不卡死）；
        auto 约 0.37s/页 ⇒ 放工作线程算，完了经 _post_ui 回主线程刷新 QPixmap。
        """
        self._exposure_token += 1
        token = self._exposure_token
        params = self.exposure_panel.exposure_dict()
        if not params or params.get("mode") != ExposurePanel.MODE_AUTO:
            self._show_image()
            return
        rotation = self._rotation
        threading.Thread(target=self._auto_worker,
                         args=(token, params, rotation), daemon=True).start()

    def _auto_worker(self, token, params, rotation):
        """工作线程：全分辨率 auto 曝光（约 0.37s/页），不碰任何控件。"""
        img = None
        err = None
        try:
            img = self._load_and_expose(params)
            if rotation:
                img = img.rotate(rotation, expand=True)
        except Exception as e:
            err = e
            img = None
        self._post_ui(lambda: self._on_auto_result(token, img, err))

    def _on_auto_result(self, token, img, err):
        """主线程：接收 auto 重算结果。过期结果直接丢弃（否则旧结果会盖掉新结果）。"""
        if token != self._exposure_token:
            return
        if img is None:
            self.info_label.setText(f"预览失败: {err}")
            return
        self._set_preview(img)

    def _load_and_expose(self, params):
        """读当前页（裁边后读裁边件）并应用曝光（顺序固定：曝光 → 旋转）。"""
        with Image.open(self._source_path()) as src:
            img = src.copy()
        return apply_exposure(img, **params)

    # ────────── 渲染 ──────────
    def _show_image(self):
        """同步渲染预览（曝光 → 旋转）。"""
        try:
            params = self.exposure_panel.exposure_dict()
            with Image.open(self._source_path()) as src:
                img = src.copy()
            if params:
                img = apply_exposure(img, **params)
            if self._rotation:
                img = img.rotate(self._rotation, expand=True)
        except Exception as e:
            self.info_label.setText(f"预览失败: {e}")
            return
        self._set_preview(img)

    def _set_preview(self, img):
        """把 PIL 图像刷到预览区（PIL 已旋转，不重复旋转）。"""
        try:
            pixmap = pil_to_qpixmap(img)
            self.image_viewer.set_image(pixmap, rotation=0)  # PIL已旋转，不重复
            w, h = img.size
            rot_text = f" (旋转 {self._rotation}°)" if self._rotation else ""
            self.info_label.setText(f"{w} × {h}{rot_text}")
        finally:
            img.close()

    def _rotate_left(self):
        self._rotation = (self._rotation - 90) % 360
        self._sync_page_meta()
        self._recompute_preview()

    def _rotate_right(self):
        self._rotation = (self._rotation + 90) % 360
        self._sync_page_meta()
        self._recompute_preview()

    # ────────── 裁边 / 元数据 ──────────
    def _source_path(self):
        """当前真正使用的页文件：裁边后用裁边件，否则用缓存原件。"""
        if self._crop_path and os.path.exists(self._crop_path):
            return self._crop_path
        return self.cache_path

    def _crop_path_for(self, source_path):
        """
        裁边结果落盘路径：与原件同目录、同扩展名，文件名加 `crop_` 前缀。

        ★ 绝不覆盖缓存原件（page_meta 的契约：cache_path 指向永不修改的原始扫描件）
        ★ 文件名不以 `page_` 开头 ⇒ 不会被 cache_manager.list_pages 当成缓存页
        """
        d = os.path.dirname(source_path)
        stem = os.path.splitext(os.path.basename(source_path))[0]
        return os.path.join(d, f"crop_{stem}.{self.ext}")

    def _auto_crop(self):
        """
        自动裁边。

        ★ 不再覆盖缓存原件：结果写到 crop_ 前缀的新文件，原件保持只读
        ★ 格式按**原扩展名**决定（.png ⇒ PNG，.jpg ⇒ JPEG quality=95），不再一律 JPEG
        ★ 写盘用临时文件 + os.replace（原子），中途失败不会毁掉已有文件
        """
        cache = self.cache_path
        try:
            if not cache or not os.path.exists(cache):
                self.info_label.setText("裁边失败: 缓存文件不存在")
                return
            with open(cache, "rb") as f:
                raw = f.read()
            with Image.open(io.BytesIO(raw)) as src:
                img = src.copy()
            cropped = None
            try:
                cropped = auto_crop(img)
                if cropped is img:
                    self.info_label.setText("无需裁边")
                    return
                rect = _detect_crop_rect(img)
                if rect is not None:
                    rw, rh = rect[2] - rect[0], rect[3] - rect[1]
                    if (rw, rh) != cropped.size:
                        rect = None          # 算不准就不写，别写错
                crop_path = self._crop_path_for(cache)
                _write_image_atomic(cropped, crop_path, self.ext)
            finally:
                if cropped is not None and cropped is not img:
                    cropped.close()
                img.close()
            self._crop_path = crop_path
            self._crop_rect = rect
            self._cropped = True
            self._rotation = 0
            self._sync_page_meta()
            self._recompute_preview()
        except Exception as e:
            logger.debug("自动裁边失败: %s", e, exc_info=True)
            self.info_label.setText(f"裁边失败: {type(e).__name__}")

    def _sync_page_meta(self):
        """
        把当前几何信息写回 page_meta。

        裁边改了图像尺寸、旋转改了朝向 ⇒ 元数据必须跟上，
        否则 page_meta 记的还是扫描时的原图尺寸，纠偏/续扫的回溯链就断了。
        （page_meta 只**调用**，不改它的实现）
        """
        try:
            source = self._source_path()
            with Image.open(source) as im:
                width, height = im.size
            store = page_meta.PageMetaStore(cache_manager._cache_root())
            existing = {}
            try:
                existing = store.get_job(self.job_id).get("1", {}) or {}
            except Exception:
                existing = {}
            store.set_page(self.job_id, 1, page_meta.PageMeta(
                cache_path=self.cache_path,
                width=int(width), height=int(height),
                manual_rotation=int(self._rotation),
                deskew_angle=float(existing.get("deskew_angle") or 0.0),
                a4_normalized=bool(existing.get("a4_normalized", False)),
                crop_rect=tuple(self._crop_rect) if self._crop_rect else None,
            ))
        except Exception as e:
            logger.debug("写页元数据失败 [%s]: %s", self.job_id, e)

    def _confirm(self):
        """保存"""
        exposure = self.exposure_panel.exposure_dict()
        source = self._source_path()
        try:
            saved_files = _save_page_specs(
                self.job_id, self.ext, self.output_path,
                [(source, self._rotation)], [[0]],
                extra_dirs=self.extra_dirs, device_name=self.device_name,
                device_ip=self.device_ip, exposure=exposure)
            # 曝光设置回写给主窗口 —— 仅本次扫描内有效（下一次扫描开始时清空）
            parent = self.parent()
            if parent is not None and hasattr(parent, "set_exposure_state"):
                parent.set_exposure_state(exposure)
            self.accept()
            if saved_files:
                try:
                    os.startfile(os.path.dirname(saved_files[0]))
                except Exception:
                    pass
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))

    # ────────── 关闭时的 job 注销 ──────────
    def _release_job(self):
        """
        解除 job 的缓存保护（幂等）。

        落盘成功 → save_manager.remove_job 已经清过；
        用户放弃（重新扫描 / 关窗）→ 这里清，否则 job 永远受保护，
        缓存目录永远不被 eviction 回收 ⇒ 缓存上限机制被架空。
        """
        _release_job_protection(self.job_id)

    def accept(self):
        self._release_job()
        super().accept()

    def reject(self):
        self._release_job()
        super().reject()


def adjust_splits_after_delete(splits, page_idx: int):
    """
    删除第 page_idx 页（0 基索引）后调整分组分割点。

    语义与 scan_coordinator.delete_cached_page 完全一致：
      - sp <  page_idx → 保留（该分割点之前的页不受影响）
      - sp == page_idx → 移除（分割点正好落在被删页上）
      - sp >  page_idx → 前移 1（被删页之后的页号整体前移，分割点同步前移）

    splits 与 page_idx 都是「页列表下标」空间的量，与协调器一致。
    """
    new_splits = set()
    for sp in splits:
        if sp < page_idx:
            new_splits.add(sp)
        elif sp > page_idx:
            new_splits.add(sp - 1)
    return new_splits


class MultiPagePreviewDialog(QDialog, _UiPostMixin):
    """多页预览对话框（UI 优化版）"""

    TW, TH = 80, 100

    def __init__(self, parent, job_id, ext, output_path, page_count,
                 device_name="", device_ip="", extra_dirs=None, exposure=None):
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
        # 裁边结果：页号 → 文件路径（不覆盖缓存原件）
        self._crop_paths = {}
        self._crop_rects = {}
        # 保存中（后台线程落盘期间禁止重复提交）
        self._saving = False
        self._save_finished = False
        self._save_exposure = None
        self._save_total_pages = 0
        self._save_group_count = 1
        # 递增 token：auto 重算完成后用它判断结果是否已过期
        self._exposure_token = 0
        self._initial_exposure = exposure

        self.setWindowTitle("多页预览")
        self.setMinimumSize(720, 520)
        self.resize(820, 620)

        # C++ 对象是否还活着（destroyed 信号是唯一可靠的判据）
        self._alive = True
        self.destroyed.connect(self._mark_destroyed)
        _ensure_ui_dispatcher()
        self._setup_ui()
        self._update_preview()
        self._refresh_thumbs()
        add_fade_animation(self, ANIM["normal"])

    # ────────── 线程 / 生命周期 ──────────
    def _post_ui(self, fn):
        """
        投递到主线程。

        ★ 走**模块级**投递器，不用本对话框自己的 `_ui_post` 信号：
          后台线程（曝光重算 / 后台保存）可能在对话框已被销毁之后才投递，
          往已销毁的 QObject 上 emit 会踩野指针 ⇒ 进程堆损坏（0xc0000374）。
        """
        _post_to_ui(fn)

    def _mark_destroyed(self, *args):
        """C++ 对象已析构 ⇒ 之后不能再碰它的任何控件。"""
        self._alive = False

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

        # 曝光工具条（文档级参数：作用于全部页；可折叠，默认收起）
        self.exposure_panel = ExposurePanel(self, exposure=self._initial_exposure)
        self.exposure_panel.params_changed.connect(self._on_exposure_changed)
        layout.addWidget(self.exposure_panel)

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

        # 分组线入口（I-5）：在此页开始新组 / 取消分组
        self.split_btn = AnimatedButton("＋分组线", button_type="secondary")
        self.split_btn.setToolTip("从当前页开始新的一组（再点一次取消）")
        self.split_btn.clicked.connect(self._toggle_split)
        btn_layout.addWidget(self.split_btn)

        btn_layout.addStretch()

        cancel_btn = AnimatedButton("重新扫描", button_type="secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = AnimatedButton("保存全部", button_type="primary")
        save_btn.clicked.connect(self._confirm)
        btn_layout.addWidget(save_btn)
        self.save_btn = save_btn

        layout.addLayout(btn_layout)

    def _cache_path(self, page_num):
        return os.path.join(cache_manager._job_dir(self.job_id), f"page_{page_num:03d}.{self.ext}")

    def _source_path(self, page_num):
        """当前真正使用的页文件：裁边后用裁边件，否则用缓存原件。"""
        cropped = self._crop_paths.get(page_num)
        if cropped and os.path.exists(cropped):
            return cropped
        return self._cache_path(page_num)

    def _crop_path_for(self, page_num):
        """
        裁边结果落盘路径：与原件同目录、同扩展名，文件名加 `crop_` 前缀。

        ★ 绝不覆盖缓存原件（page_meta 的契约：cache_path 指向永不修改的原始扫描件）
        ★ 文件名不以 `page_` 开头 ⇒ 不会被 cache_manager.list_pages 当成缓存页
        """
        d = os.path.dirname(self._cache_path(page_num))
        return os.path.join(d, f"crop_page_{page_num:03d}.{self.ext}")

    def _get_rotation(self, page_num):
        return self._rotations.get(page_num, 0)

    # ────────── 分组（I-5） ──────────
    def _toggle_split(self):
        """
        在当前页处切换"分组线"。

        splits 的语义：**新一组的第一页在页列表里的下标**（0 基）。
        ⇒ 第 1 页（下标 0）不能作为分割点（它本来就是第一组的开头）。
        """
        if not self.pages:
            return
        idx = self.selected_idx
        if idx <= 0:
            self.page_info_label.setText("第 1 页不能作为分组起点")
            return
        if idx in self.splits:
            self.splits.discard(idx)
        else:
            self.splits.add(idx)
        self._refresh_thumbs()
        self._update_split_button()

    def _update_split_button(self):
        """分组按钮的文案跟着当前页走（有分割线 ⇒ 显示取消）。"""
        if not hasattr(self, "split_btn"):
            return
        if self.selected_idx in self.splits and self.selected_idx > 0:
            self.split_btn.setText("－分组线")
            self.split_btn.setToolTip("取消当前页处的分组线")
        else:
            self.split_btn.setText("＋分组线")
            self.split_btn.setToolTip("从当前页开始新的一组（再点一次取消）")

    def _group_index_of(self, idx):
        """当前页属于第几组（从 1 起）。"""
        group = 1
        for sp in sorted(self.splits):
            if sp <= idx:
                group += 1
            else:
                break
        return group

    # ────────── 曝光 ──────────
    def _on_exposure_changed(self):
        """曝光参数变化 → 重算预览（文档级参数，作用于全部页）。"""
        self._recompute_preview()

    def _recompute_preview(self):
        """
        按当前曝光参数重算当前页预览。

        manual 约 **0.130s/页**（300dpi A4 主线程实测：decode 0.026s
        + apply_exposure 0.065s + QPixmap 0.039s）⇒ 同步重算，量级可接受；
        auto（约 0.37s/页）放工作线程算，完了经 _post_ui 回主线程刷新 QPixmap。
        """
        self._exposure_token += 1
        token = self._exposure_token
        params = self.exposure_panel.exposure_dict()
        if not params or params.get("mode") != ExposurePanel.MODE_AUTO:
            self._update_preview()
            return
        page_num = self.pages[self.selected_idx]
        rotation = self._get_rotation(page_num)
        threading.Thread(target=self._auto_worker,
                         args=(token, params, page_num, rotation), daemon=True).start()

    def _auto_worker(self, token, params, page_num, rotation):
        """工作线程：全分辨率 auto 曝光（约 0.37s/页），不碰任何控件。"""
        img = None
        err = None
        try:
            img = self._load_and_expose(params, page_num)
            if rotation:
                img = img.rotate(rotation, expand=True)
        except Exception as e:
            err = e
            img = None
        self._post_ui(lambda: self._on_auto_result(token, img, err))

    def _on_auto_result(self, token, img, err):
        """主线程：接收 auto 重算结果。过期结果直接丢弃（否则旧结果会盖掉新结果）。"""
        if token != self._exposure_token:
            return
        if img is None:
            self.page_info_label.setText(f"预览失败: {err}")
            return
        self._set_preview(img)

    def _load_and_expose(self, params, page_num):
        """读当前页（裁边后读裁边件）并应用曝光（顺序固定：曝光 → 旋转）。"""
        with Image.open(self._source_path(page_num)) as src:
            img = src.copy()
        return apply_exposure(img, **params)

    # ────────── 渲染 ──────────
    def _update_preview(self):
        """同步渲染当前页（曝光 → 旋转）。"""
        try:
            params = self.exposure_panel.exposure_dict()
            page_num = self.pages[self.selected_idx]
            with Image.open(self._source_path(page_num)) as src:
                img = src.copy()
            if params:
                img = apply_exposure(img, **params)
            rotation = self._get_rotation(page_num)
            if rotation:
                img = img.rotate(rotation, expand=True)
        except Exception as e:
            self.page_info_label.setText(f"预览失败: {e}")
            return
        self._set_preview(img)

    def _set_preview(self, img):
        """把 PIL 图像刷到预览区（PIL 已旋转，不重复旋转）。"""
        try:
            pixmap = pil_to_qpixmap(img)
            self.image_viewer.set_image(pixmap, rotation=0)  # PIL已旋转
            w, h = img.size
            rotation = 0
            if 0 <= self.selected_idx < len(self.pages):
                rotation = self._get_rotation(self.pages[self.selected_idx])
            rot_text = f" (旋转 {rotation}°)" if rotation else ""
            group_text = ""
            if self.splits:
                group_text = f" · 第 {self._group_index_of(self.selected_idx)} 组"
            self.page_info_label.setText(
                f"第 {self.selected_idx + 1} 页 / 共 {len(self.pages)} 页"
                f"{group_text}{rot_text}")
        finally:
            img.close()

    def _refresh_thumbs(self):
        for i in reversed(range(self.thumbs_layout.count())):
            widget = self.thumbs_layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()

        for idx, page_num in enumerate(self.pages):
            # 分组线：在该页缩略图上方画一条分隔线（用户要看得见分组真的存在）
            if idx in self.splits:
                sep = QFrame()
                sep.setFrameShape(QFrame.HLine)
                sep.setFixedHeight(3)
                sep.setStyleSheet(f"background: {COLORS['primary']}; border: none;")
                self.thumbs_layout.addWidget(sep)
            try:
                img = Image.open(self._source_path(page_num))
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
        self._update_split_button()

    def _on_thumb_clicked(self, idx):
        self.selected_idx = idx
        self._recompute_preview()
        self._refresh_thumbs()

    def _rotate_left(self):
        page_num = self.pages[self.selected_idx]
        self._rotations[page_num] = (self._get_rotation(page_num) - 90) % 360
        self._sync_page_meta(page_num)
        self._recompute_preview()
        self._refresh_thumbs()

    def _rotate_right(self):
        page_num = self.pages[self.selected_idx]
        self._rotations[page_num] = (self._get_rotation(page_num) + 90) % 360
        self._sync_page_meta(page_num)
        self._recompute_preview()
        self._refresh_thumbs()

    # ────────── 裁边 / 元数据 ──────────
    def _crop_page(self, page_num):
        """
        对一页做自动裁边。

        ★ 不再覆盖缓存原件：结果写到 crop_ 前缀的新文件，原件保持只读
        ★ 格式按**原扩展名**决定（.png ⇒ PNG，.jpg ⇒ JPEG quality=95）
        ★ 写盘用临时文件 + os.replace（原子）
        返回 True 表示确实裁了。
        """
        source = self._cache_path(page_num)
        with open(source, "rb") as f:
            raw = f.read()
        with Image.open(io.BytesIO(raw)) as src:
            img = src.copy()
        cropped = None
        try:
            cropped = auto_crop(img)
            if cropped is img:
                return False
            rect = _detect_crop_rect(img)
            if rect is not None:
                rw, rh = rect[2] - rect[0], rect[3] - rect[1]
                if (rw, rh) != cropped.size:
                    rect = None              # 算不准就不写，别写错
            crop_path = self._crop_path_for(page_num)
            _write_image_atomic(cropped, crop_path, self.ext)
        finally:
            if cropped is not None and cropped is not img:
                cropped.close()
            img.close()
        self._crop_paths[page_num] = crop_path
        self._crop_rects[page_num] = rect
        self._cropped.add(page_num)
        self._rotations.pop(page_num, None)
        return True

    def _sync_page_meta(self, page_num):
        """
        把裁边/旋转后的几何信息写回 page_meta（原始件不动，元数据要跟上）。
        （page_meta 只**调用**，不改它的实现）
        """
        try:
            source = self._source_path(page_num)
            with Image.open(source) as im:
                width, height = im.size
            store = page_meta.PageMetaStore(cache_manager._cache_root())
            existing = {}
            try:
                existing = store.get_job(self.job_id).get(str(page_num), {}) or {}
            except Exception:
                existing = {}
            crop_rect = self._crop_rects.get(page_num)
            store.set_page(self.job_id, page_num, page_meta.PageMeta(
                cache_path=self._cache_path(page_num),
                width=int(width), height=int(height),
                manual_rotation=int(self._get_rotation(page_num)),
                deskew_angle=float(existing.get("deskew_angle") or 0.0),
                a4_normalized=bool(existing.get("a4_normalized", False)),
                crop_rect=tuple(crop_rect) if crop_rect else None,
            ))
        except Exception as e:
            logger.debug("写页元数据失败 [%s]: %s", self.job_id, e)

    def _auto_crop(self):
        page_num = self.pages[self.selected_idx]
        try:
            if self._crop_page(page_num):
                self._sync_page_meta(page_num)
                self._recompute_preview()
                self._refresh_thumbs()
        except Exception as e:
            logger.debug("自动裁边失败: %s", e, exc_info=True)
            self.page_info_label.setText(f"裁边失败: {e}")

    def _auto_crop_all(self):
        for page_num in self.pages:
            try:
                if self._crop_page(page_num):
                    self._sync_page_meta(page_num)
            except Exception:
                pass
        self._recompute_preview()
        self._refresh_thumbs()

    def _delete_current_page(self):
        """删除当前页"""
        if len(self.pages) <= 1:
            QMessageBox.warning(self, "提示", "至少需要保留一页")
            return

        page_num = self.pages[self.selected_idx]
        deleted_idx = self.selected_idx
        reply = QMessageBox.question(
            self, "确认", f"确定要删除第 {self.selected_idx + 1} 页吗？")
        if reply != QMessageBox.Yes:
            return

        # 删除缓存文件
        try:
            cache_path = self._cache_path(page_num)
            if os.path.exists(cache_path):
                os.remove(cache_path)
            # 裁边件也一并删掉（它是这页的衍生物）
            crop_path = self._crop_paths.pop(page_num, None)
            self._crop_rects.pop(page_num, None)
            if crop_path and os.path.exists(crop_path):
                os.remove(crop_path)
        except Exception:
            pass

        # 从列表中移除
        self.pages.pop(deleted_idx)
        self._rotations.pop(page_num, None)
        self._cropped.discard(page_num)

        # 分组边界同步调整：页号前移 ⇒ 分割点前移；落在被删页上的分割点移除
        # （与 scan_coordinator.delete_cached_page 语义一致）
        self.splits = adjust_splits_after_delete(self.splits, deleted_idx)

        # 调整选中索引
        if self.selected_idx >= len(self.pages):
            self.selected_idx = len(self.pages) - 1

        self._recompute_preview()
        self._refresh_thumbs()

    def _confirm(self):
        """
        保存全部。

        ★ 落盘挪到后台线程（F-5）：几百页合并要几十秒，在主线程做会把界面卡死。
          完成/失败都回主线程再提示（模块级投递器，不依赖本对话框的信号）。
        ★ 用 save_manager.run_save_job（纯函数入口，无 Qt 依赖）
        ★ 裁过边的页用裁边件（原件保持只读）
        ★ 分组由 splits 决定，命名走 save_manager.build_output_name 的多组规则
        """
        if getattr(self, "_saving", False):
            return
        exposure = self.exposure_panel.exposure_dict()
        page_specs = [(self._source_path(page_num), self._get_rotation(page_num))
                      for page_num in self.pages]
        groups = self._build_groups()
        total_pages = len(page_specs)
        self._saving = True
        self._save_exposure = exposure
        self._save_total_pages = total_pages
        self._save_group_count = len(groups)
        if hasattr(self, "save_btn"):
            self.save_btn.setEnabled(False)
            self.save_btn.setText("保存中...")

        # SaveRequest 只装数据，不带 GUI 对象 ⇒ 可以安全跨线程传递
        request = save_manager.SaveRequest(
            job_id=self.job_id,
            ext=self.ext,
            output_path=self.output_path,
            page_specs=page_specs,
            groups=groups,
            extra_dirs=self.extra_dirs,
            device_name=self.device_name,
            device_ip=self.device_ip,
            exposure=exposure,
            confirm=self._make_confirm_callback(),
        )

        def _work():
            return save_manager.run_save_job(request)

        # 投递器在主线程创建（跨线程 emit 的接收端必须活在主线程）
        _ensure_ui_dispatcher()
        _run_in_background(
            _work,
            lambda files: self._on_save_done(files),
            lambda err: self._on_save_error(err),
        )

    def _make_confirm_callback(self):
        """
        PDF 体积预检的确认回调（save_manager 在**后台线程**里调它）。

        ★ 弹窗必须在主线程 ⇒ 回主线程弹，并阻塞等待用户回答。
        ★ 不拿本对话框当 parent（它可能已被销毁 ⇒ 野指针 ⇒ 堆损坏）。
        """
        def _confirm(msg):
            answer = {"ok": True}
            done = threading.Event()

            def _ask():
                try:
                    reply = QMessageBox.question(
                        None, "保存确认", msg,
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                    answer["ok"] = (reply == QMessageBox.Yes)
                except Exception as e:
                    logger.debug("体积预检确认失败: %s", e)
                    answer["ok"] = True
                finally:
                    done.set()

            _post_to_ui(_ask)
            done.wait(timeout=300)
            return answer.get("ok", True)

        return _confirm

    def _on_save_done(self, saved_files):
        """主线程：保存完成。"""
        if not getattr(self, "_alive", True):
            # 对话框已被销毁（用户提前关窗）—— 不能再碰它的任何控件
            logger.debug("保存完成时对话框已销毁，忽略提示")
            return
        try:
            self._saving = False
            if hasattr(self, "save_btn"):
                self.save_btn.setEnabled(True)
                self.save_btn.setText("保存全部")
            if not saved_files:
                self.page_info_label.setText("保存未产生文件")
                self._save_finished = True
                return
            failed_pages = int(getattr(saved_files, "failed_pages", 0) or 0)
            warnings = list(getattr(saved_files, "warnings", []) or [])
            total_pages = getattr(self, "_save_total_pages", len(saved_files))
            summary = _format_save_summary(saved_files, total_pages,
                                           failed_pages, warnings)
            # 曝光设置回写给主窗口 —— 仅本次扫描内有效
            parent = self.parent()
            if parent is not None and hasattr(parent, "set_exposure_state"):
                parent.set_exposure_state(getattr(self, "_save_exposure", None))
            # ★ 正常完成只写状态栏（不弹窗，免得打断操作）；
            #   只有"有页没存下来 / 有警告"才弹窗 —— 那才是必须让用户看见的。
            status = f"已保存 {len(saved_files)} 个文件"
            if failed_pages:
                status += f"，{failed_pages} 页保存失败"
            if parent is not None and hasattr(parent, "status_bar"):
                try:
                    parent.status_bar.showMessage(status)
                except Exception:
                    pass
            if failed_pages or warnings:
                # ★ 不拿本对话框当 parent（它可能已被销毁 ⇒ 野指针 ⇒ 堆损坏）
                QMessageBox.warning(
                    None, "保存部分失败" if failed_pages else "保存提示", summary)
            self.accept()
            try:
                os.startfile(os.path.dirname(saved_files[0]))
            except Exception:
                pass
        except RuntimeError:
            # 对话框已被销毁（用户提前关窗）—— 不用再提示
            logger.debug("保存完成时对话框已销毁")
        finally:
            self._save_finished = True

    def _on_save_error(self, err):
        """主线程：保存失败。"""
        if not getattr(self, "_alive", True):
            logger.debug("保存失败时对话框已销毁，忽略提示")
            return
        try:
            self._saving = False
            if hasattr(self, "save_btn"):
                self.save_btn.setEnabled(True)
                self.save_btn.setText("保存全部")
            # ★ 不拿本对话框当 parent（它可能已被销毁 ⇒ 野指针 ⇒ 堆损坏）
            QMessageBox.critical(None, "保存失败", str(err))
        except RuntimeError:
            logger.debug("保存失败时对话框已销毁")
        finally:
            self._save_finished = True

    # ────────── 关闭时的 job 注销 ──────────
    def _release_job(self):
        """
        解除 job 的缓存保护（幂等）。

        落盘成功 → save_manager.remove_job 已经清过；
        用户放弃（重新扫描 / 关窗）→ 这里清，否则 job 永远受保护，
        缓存目录永远不被 eviction 回收 ⇒ 缓存上限机制被架空。
        """
        _release_job_protection(self.job_id)

    def accept(self):
        self._release_job()
        super().accept()

    def reject(self):
        self._release_job()
        super().reject()

    def _build_groups(self):
        """分组计算统一走协调器实现（scan_coordinator.compute_page_groups）

        与协调器同源 ⇒ GUI 与协调器的分组语义不会再分叉。
        """
        return scan_coordinator.compute_page_groups(self.splits, len(self.pages))


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
