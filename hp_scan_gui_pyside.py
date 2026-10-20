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
import time
from datetime import datetime
from typing import Optional

from PIL import Image

# ★★ PIL 的编码插件是**惰性加载**的（`Image.save()` 首次用到某格式时才 import
#   对应插件）。本文件的「直接保存」与「会话落盘」都在**后台线程**里写文件 ⇒
#   若第一次 `Image.save()` 恰好发生在后台线程，插件的懒加载会与 GC 撞车，
#   进程直接 `Fatal Python error: Aborted`（整轮测试/整个应用被杀）。
#   ⇒ 在**主线程**导入时先把插件预热掉（一次性成本，几十毫秒）。
try:
    Image.init()
except Exception:            # pragma: no cover - 预热失败不该挡住启动
    pass

import cache_manager
import doc_merge
import document_session
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
from escl_engine import auto_crop, ScanCancelled

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
    QRect, QObject, QMutexLocker,
    QEventLoop,
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
    QRadioButton, QListWidget, QListWidgetItem,
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


def _rotated_size(size, rotation):
    """
    旋转后的图像尺寸（与 PIL `rotate(angle, expand=True)` 一致）。

    90° / 270° ⇒ 宽高对调；180° / 0° ⇒ 不变。
    """
    w, h = int(size[0]), int(size[1])
    if int(rotation or 0) % 180 == 90:
        return (h, w)
    return (w, h)


def _rotate_rect(rect, rotation, size):
    """
    把裁边框从**原图坐标系**变换到**旋转后的坐标系**（F-2）。

    PIL 的 `img.rotate(angle, expand=True)` 是**逆时针**旋转，像素映射为：
      90° : (x, y) → (y, W-1-x)        画布变为 (H, W)
      180°: (x, y) → (W-1-x, H-1-y)    画布不变
      270°: (x, y) → (H-1-y, x)        画布变为 (H, W)

    rect = (left, top, right, bottom)，right/bottom 为**开区间**；
    size = 原图尺寸 (W, H)。
    返回旋转后坐标系里的裁边框（宽高随 90/270 对调）。
    """
    if not rect:
        return None
    left, top, right, bottom = rect
    w, h = int(size[0]), int(size[1])
    angle = int(rotation or 0) % 360
    if angle == 90:
        return (top, w - right, bottom, w - left)
    if angle == 180:
        return (w - right, h - bottom, w - left, h - top)
    if angle == 270:
        return (h - bottom, left, h - top, right)
    return (left, top, right, bottom)


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


def _run_in_background(work, on_done, on_error, label="background"):
    """
    把 work 丢到后台线程跑；完成/失败都回**主线程**回调。

    用普通的 threading.Thread（和本文件里曝光重算的工作线程同一套写法），
    不用 QThreadPool/QRunnable：autoDelete 的 QRunnable 被 Qt 回收后，
    Python 包装对象再被 GC 会二次释放 ⇒ 进程 heap corruption。

    ★ 线程**不是 daemon**（复审 A1-R2）：
      daemon 线程在进程退出时被**强杀** ⇒ 用户在落盘**中途**关窗 ⇒ 磁盘上留下
      **截断**的 PDF（能打开、页数却不对）。非 daemon 线程会被 Python 在退出前
      join 掉 ⇒ 写完才退。配套：`ScanApp.closeEvent` 里**有界等待**在途任务。
    ★ 每个线程都登记进 `_BACKGROUND_TASKS` ⇒ 关窗时知道还有谁在跑。
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
        finally:
            _unregister_background_task(threading.current_thread())

    thread = threading.Thread(target=_runner, daemon=False)
    _register_background_task(thread, label)
    thread.start()
    return thread


# ────────── 后台任务登记（关窗时等落盘用）──────────
#
# ★ 复审 A1-R2：落盘线程是 daemon ⇒ 用户在落盘**中途**关窗 ⇒ 进程被直接杀掉
#   ⇒ 磁盘上留下**截断**的 PDF（能打开、页数却不对）。
#   两道保险：
#     ① 落盘线程**不是 daemon** ⇒ 进程退出前 Python 会 join 它（写完才退）；
#     ② `ScanApp.closeEvent` 里**有界等待**在途任务，等不到就**阻止关闭**。
_BACKGROUND_TASKS: "dict[threading.Thread, str]" = {}
_BACKGROUND_TASKS_LOCK = threading.Lock()


def _register_background_task(thread, label):
    """登记在途后台任务（关窗时按这张表等待）。"""
    with _BACKGROUND_TASKS_LOCK:
        _BACKGROUND_TASKS[thread] = label


def _unregister_background_task(thread):
    """注销已结束的后台任务（幂等）。"""
    with _BACKGROUND_TASKS_LOCK:
        _BACKGROUND_TASKS.pop(thread, None)


def pending_background_tasks():
    """在途后台任务 [(线程, 标签)]。"""
    with _BACKGROUND_TASKS_LOCK:
        return [(t, lbl) for t, lbl in _BACKGROUND_TASKS.items() if t.is_alive()]


class _SaveTracker:
    """
    在途保存任务登记（按标签计数）。

    ★ 为什么不能是单个布尔标志（复审 A1-R1）：
      落盘后台化之后，**可能有多个保存同时在飞**（用户扫完一份、落盘还没结束又扫一份）。
      老的 `self._direct_save_finished` 是**一个布尔量**：第二次开始时把它置 False，
      而**先结束的那次**的回调会把它置 True ⇒ 第二个保存还在跑，标志却已经变成
      "全部完成"。这里用「标签 → 计数」：几个在跑就减几个。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict = {}

    def begin(self, label: str) -> None:
        """登记一次在途保存。"""
        with self._lock:
            self._counts[label] = self._counts.get(label, 0) + 1

    def end(self, label: str) -> None:
        """注销一次在途保存（多减不会变成负数）。"""
        with self._lock:
            n = self._counts.get(label, 0) - 1
            if n > 0:
                self._counts[label] = n
            else:
                self._counts.pop(label, None)

    def count(self, label=None) -> int:
        """在途保存数（label=None ⇒ 全部标签合计）。"""
        with self._lock:
            if label is None:
                return sum(self._counts.values())
            return self._counts.get(label, 0)

    def all_done(self, label=None) -> bool:
        """是否**全部**保存都已结束。"""
        return self.count(label) == 0


def _save_tracker_of(app):
    """
    取某个 app 的在途保存计数器（没有就懒建一个）。

    ★ 做成模块级函数而不是方法：本文件里 `_direct_save_result` 之类的方法会被
      既有测试的**最小替身**借用（替身只有那几个方法）⇒ 不能假设替身上
      也有本文件新加的方法。
    """
    tracker = getattr(app, "_save_tracker", None)
    if not isinstance(tracker, _SaveTracker):
        tracker = _SaveTracker()
        try:
            app._save_tracker = tracker
        except Exception:       # pragma: no cover - 极端替身（属性不可写）
            pass
    return tracker


def _pending_save_count(app, label=None) -> int:
    """在途保存任务数（label=None ⇒ 全部标签合计）。"""
    return _save_tracker_of(app).count(label)


def _refresh_scan_button(app):
    """
    按「落盘在途」刷新扫描按钮状态。

    ★ 复审 A1-R1：直接保存后台化之后，**落盘期间扫描按钮被重新启用**
      ⇒ 用户能再点一次扫描 ⇒ 两个 save_pages 并发重叠（产物还可能互相覆盖）。
      这里把「落盘在途」当成一等状态：落盘结束前按钮保持禁用（文案也换掉，
      让用户知道界面为什么点不动）。
    ★ 扫描进行中也不能点（与改造前的行为一致）。
    """
    btn = getattr(app, "scan_btn", None)
    if btn is None:
        return
    pending = _pending_save_count(app)
    scanning = bool(getattr(app, "_scan_in_progress", False))
    try:
        if pending:
            btn.setText("保存中...")
            btn.setEnabled(False)
        elif scanning:
            btn.setText("扫描中...")
            btn.setEnabled(False)
        else:
            btn.setText("扫描")
            btn.setEnabled(True)
    except RuntimeError:       # pragma: no cover - 控件已销毁
        logger.debug("刷新扫描按钮状态失败（控件已销毁）")


class _OutputNameGuard:
    """
    输出文件名唯一性守卫（GUI 侧兜底）。

    ★ 复审 A1-R1 ③ 的由来：当时协调器给的结果文件名精度**只到秒**
      ⇒ 同一秒内完成的两批会算出**同一个文件名** ⇒ 后写的覆盖先写的。
      当时协调器不在本文件的修改范围内 ⇒ GUI 侧兜底：给每批输出占一个
      **没被占用**的 base（`{base}` → `{base}_1` → `{base}_2` …）。
    ★ 现状（2026-10-21）：协调器侧的文件名精度已提到**毫秒**
      （`scan_coordinator.py` 的 `HP_Scan_%Y%m%d_%H%M%S_%f`）⇒ 跨进程
      撞名概率已极低；但本守卫**继续保留**：
        - 同一毫秒内完成的两批（大批量扫描可能出现）仍会撞；
        - 兜底是**跨实例共享**（模块级单例）+ 查磁盘 ⇒ 重名时改用 `{base}_1`，
          而协调器的毫秒名会把上一次产物整个覆盖掉。
      所以守卫不是冗余，是最后一道防线。
    ★ 占用记录**跨实例共享**（模块级单例）⇒ 同一进程里两次落盘不会撞名。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._claimed = set()

    def claim(self, output_path):
        """给一批输出占一个唯一的 base，返回可用的输出路径。"""
        if not output_path:
            return output_path
        base, ext = os.path.splitext(output_path)
        with self._lock:
            candidate = base
            n = 0
            while self._taken(candidate):
                n += 1
                candidate = f"{base}_{n}"
            self._claimed.add(candidate)
        return candidate + ext

    def _taken(self, base):
        """这个 base 是不是已经有人要了 / 磁盘上已经有东西了。"""
        if base in self._claimed:
            return True
        directory = os.path.dirname(base) or "."
        prefix = os.path.basename(base)
        try:
            names = os.listdir(directory)
        except OSError:
            return False
        for name in names:
            # 产物名要么是 `{base}.{ext}`，要么是 `{base}_001.{ext}` /
            # `{base}_001_002.{ext}`（多组多页）⇒ 认前缀即可
            if (name == prefix or name.startswith(prefix + ".")
                    or name.startswith(prefix + "_")):
                return True
        return False


OUTPUT_NAME_GUARD = _OutputNameGuard()


def _unique_output_path(output_path):
    """输出路径唯一化（同一秒完成的两批不会互相覆盖）。"""
    return OUTPUT_NAME_GUARD.claim(output_path)


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


# ────────── 文档会话（批次 4 · B4-6）──────────
#
# 分三层：
#   ① **纯逻辑**（本节）—— 不依赖 Qt 窗口，可直接单测
#   ② **弹窗**（SkewWarningDialog / SessionMergePreviewDialog）
#   ③ **接线**（ScanApp 里的方法）
#
# ★ 合并算法一律**委托 doc_merge**（本文件不重写合并规则）。

#: 扫描模式（与 document_session.MODES 对齐）
SESSION_MODE_SINGLE = "single"
SESSION_MODE_DUPLEX = "duplex"
SESSION_MODE_BATCH = "batch"

#: 模式 → 界面文案
SESSION_MODE_TEXT = {
    SESSION_MODE_SINGLE: "单面",
    SESSION_MODE_DUPLEX: "双面（手动）",
    SESSION_MODE_BATCH: "加入现有文档（续扫）",
}

#: 会话状态 → 界面文案
SESSION_STATE_TEXT = {
    "scanning": "扫描中",
    "awaiting_flip": "待翻面",
    "paused": "已暂停",
    "completed": "已完成",
    "abandoned": "已放弃",
}

#: 单选按钮样式（与 resources/styles/app.qss 里的 QCheckBox 指示器同色系
#: ⇒ 新控件贴现有风格，**不动既有控件的样式**）
_RADIO_QSS = """
QRadioButton {
    color: #334155;
    spacing: 8px;
}
QRadioButton::indicator {
    width: 18px;
    height: 18px;
    border: 1.5px solid #CBD5E1;
    border-radius: 9px;
    background: white;
}
QRadioButton::indicator:hover {
    border-color: #0D9488;
}
QRadioButton::indicator:checked {
    background: #0D9488;
    border: 1.5px solid #0D9488;
}
"""


def _page_no_from_path(path) -> int:
    """从缓存文件名解析页号（`page_003.jpg` → 3）；解析不出来返回 0"""
    name = os.path.basename(str(path))
    if not name.startswith("page_"):
        return 0
    digits = ""
    for ch in name[5:]:
        if not ch.isdigit():
            break
        digits += ch
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def session_merged_pages(session) -> list:
    """
    会话页的**最终页序**（合并规则一律委托 doc_merge，本函数不重写算法）。

    · duplex ⇒ `interleave_duplex(front, back, reverse_back=session.reverse_back)`
      （side == "single" 的页按**正面**处理）
    · batch / single ⇒ 按 job_id 分组（首次出现顺序，组内原序）⇒ `merge_batches`

    ★ 与 `save_manager._session_merged_pages` 同规则（同一套 doc_merge 调用），
      差别只在这里返回 PageRef 供**预览**用；真正落盘的页序由 save_manager 负责。
    """
    pages = list(getattr(session, "pages", None) or [])
    mode = getattr(session, "mode", SESSION_MODE_SINGLE)
    if mode == SESSION_MODE_DUPLEX:
        front = [p for p in pages
                 if getattr(p, "side", "single") in ("front", "single")]
        back = [p for p in pages if getattr(p, "side", "single") == "back"]
        return doc_merge.interleave_duplex(
            front, back, reverse_back=bool(getattr(session, "reverse_back", True)))
    batches = []
    for p in pages:
        job_id = getattr(p, "job_id", "")
        if batches and batches[-1][0] == job_id:
            batches[-1][1].append(p)
        else:
            batches.append((job_id, [p]))
    return doc_merge.merge_batches([b[1] for b in batches])


def resume_from_page(session, job_id, page_no, drop_cache=True) -> list:
    """
    「从第 N 页重扫」—— **可复用 API**（批次 4 断点续扫地基，不是弹窗专用逻辑）。

    丢弃 `job_id` 这一批里**第 N 页及之后**的页，**保留前 N-1 页**；
    `drop_cache=True` 时同时删掉被丢页的缓存文件（页元数据交给既有孤儿清理）。

    :param session: DocumentSession（None ⇒ 空操作）
    :param job_id: 批次（job）id —— 范围**只限该批次**，其它批次不受影响
    :param page_no: 起始页号（1 基）
    :return: 被丢弃的 PageRef 列表（保持原顺序）
    """
    if session is None:
        return []
    return session.remove_pages_from(job_id, page_no, drop_cache=drop_cache)


def protect_session_jobs(session) -> None:
    """
    登记会话引用的 job 目录保护（不被 eviction / cleanup_stale 清掉）。

    ★ `cache_manager._protected_dirs` 是**进程内**集合 ⇒ 进程重启后为空
      ⇒ **跨进程恢复时必须重新登记**，否则跨天续扫的页会被
      `cleanup_stale`（24 小时过期线）删掉。
    """
    for job_id in list(getattr(session, "referenced_job_ids", []) or []):
        try:
            cache_manager.protect_dir(job_id)
        except Exception as e:
            logger.error("缓存目录保护失败 [%s]: %s", job_id, e)


def cleanup_session_jobs(session) -> None:
    """
    会话结束（落盘成功 / 用户放弃）：**先解除保护、再删缓存目录**，最后清页元数据。

    ⚠ 顺序不能反：目录还受保护时，某些清理路径会跳过它 ⇒ 永远清不掉。
    """
    job_ids = list(getattr(session, "referenced_job_ids", []) or [])
    for job_id in job_ids:
        try:
            cache_manager.unprotect_dir(job_id)
        except Exception as e:
            logger.debug("解除缓存目录保护失败 [%s]: %s", job_id, e)
    for job_id in job_ids:
        try:
            cache_manager.remove_job(job_id)
        except Exception as e:
            logger.error("删除会话缓存目录失败 [%s]: %s", job_id, e)
        try:
            page_meta.PageMetaStore(cache_manager._cache_root()).remove_job(job_id)
        except Exception as e:
            logger.debug("清页元数据失败 [%s]: %s", job_id, e)
        _release_job_protection(job_id)


def _load_thumbnail_pixmap(path, max_size=(420, 300)):
    """离屏加载缩略图（**不在弹窗里加载全尺寸大图**）；失败返回 None"""
    try:
        with Image.open(path) as src:
            img = src.convert("RGB")
            img.thumbnail(max_size)
            return pil_to_qpixmap(img)
    except Exception as e:
        logger.warning("缩略图加载失败 [%s]: %s", path, e)
        return None


class SkewWarningDialog(QDialog):
    """
    倾斜强提醒弹窗（批次 4 · B4-6）。

    ★★ 上一轮 GUI 出过 `0xc0000374`（堆损坏）：根因是**弹窗拿一个已
      `deleteLater` 的对话框当 parent**（弹窗自己的嵌套事件循环把它析构了）
      + 对话框销毁后仍往它自己的 `_ui_post` 发信号。
      ⇒ 本类的三条硬规矩：
        ① **parent 用 None**（绝不挂到任何可能先销毁的对象上）
        ② **先 exec() 弹窗、后 accept()**（按钮只在弹窗存活期间 accept）
        ③ **结果用属性 `choice` 传回，不用信号** ⇒ 对话框销毁后
           根本没有"往已销毁对象发信号"的机会
    """

    #: 中断并从第 N 页重扫
    CHOICE_RESUME = "resume"
    #: 继续扫描（自动纠偏）
    CHOICE_CONTINUE = "continue"
    #: 强制保存（不纠偏）
    CHOICE_FORCE = "force"

    def __init__(self, job_id, page_no, angle, cache_path, parent=None):
        # ★ parent 缺省 None：调用方**不要**传可能已销毁的对话框当 parent
        super().__init__(parent)
        self.setWindowTitle("页面倾斜过大")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._choice = ""
        self._job_id = str(job_id)
        self._page_no = int(page_no)
        self._angle = float(angle)
        self._cache_path = str(cache_path or "")
        self._setup_ui()

    # ── 结果 ──
    @property
    def choice(self) -> str:
        """用户的选择（空串 = 没选/关掉了弹窗）"""
        return self._choice

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING["md"])

        # 文案
        self.message_label = QLabel(
            f"第 {self._page_no} 页倾斜过大（{self._angle:.1f}°），建议重新放置后重扫。")
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 13px;")
        layout.addWidget(self.message_label)

        # 预览图（缩略图，不加载全尺寸大图）
        self.preview_label = QLabel("（无预览图）")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(180)
        self.preview_label.setStyleSheet(
            f"color: {COLORS['text_muted']}; border: 1px solid {COLORS['border']};"
            f"border-radius: {RADIUS['md']}px; background: {COLORS['surface']};")
        layout.addWidget(self.preview_label)
        pixmap = _load_thumbnail_pixmap(self._cache_path) if self._cache_path else None
        if pixmap is not None and not pixmap.isNull():
            self.preview_label.setPixmap(pixmap)

        # 三个按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(SPACING["sm"])

        self.btn_resume = AnimatedButton("中断并从第 N 页重扫", button_type="accent")
        self.btn_resume.clicked.connect(self._on_resume)
        btn_layout.addWidget(self.btn_resume)

        self.btn_continue = AnimatedButton("继续扫描（自动纠偏）", button_type="primary")
        self.btn_continue.clicked.connect(self._on_continue)
        btn_layout.addWidget(self.btn_continue)

        self.btn_force = AnimatedButton("强制保存（不纠偏）", button_type="secondary")
        self.btn_force.clicked.connect(self._on_force)
        btn_layout.addWidget(self.btn_force)

        layout.addLayout(btn_layout)

    # ── 三个按钮的行为（★ 都在弹窗存活期间设置结果，再 accept）──
    def _on_resume(self):
        self._choice = self.CHOICE_RESUME
        self.accept()

    def _on_continue(self):
        self._choice = self.CHOICE_CONTINUE
        self.accept()

    def _on_force(self):
        self._choice = self.CHOICE_FORCE
        self.accept()


class SessionMergePreviewDialog(QDialog):
    """
    合并预览（双面 / 续扫合并后给用户确认页序，再落盘）。

    ★ 同样遵守"parent 用 None + 结果用属性传回"的规矩（见 SkewWarningDialog）。
    """

    def __init__(self, session, merged_pages, duplex=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("合并预览")
        self.setModal(True)
        self.resize(560, 560)
        self._session = session
        self._pages = list(merged_pages or [])
        self._duplex = bool(duplex)
        self._setup_ui()

    @property
    def pages(self) -> list:
        """合并后的页序（PageRef 列表）"""
        return list(self._pages)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(SPACING["md"])

        front = sum(1 for p in self._pages
                    if getattr(p, "side", "single") in ("front", "single"))
        back = sum(1 for p in self._pages if getattr(p, "side", "single") == "back")
        if self._duplex:
            summary = f"共 {len(self._pages)} 页（正面 {front} 页 / 背面 {back} 页）—— 确认后合并成一个文档"
        else:
            summary = f"共 {len(self._pages)} 页 —— 确认后合并成一个文档"
        self.summary_label = QLabel(summary)
        self.summary_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        layout.addWidget(self.summary_label)

        # 页序列表
        self.page_list = QListWidget()
        self.page_list.setMinimumHeight(200)
        for idx, page in enumerate(self._pages, 1):
            side = getattr(page, "side", "single")
            side_text = {"front": "正面", "back": "背面"}.get(side, "单面")
            item = QListWidgetItem(
                f"第 {idx} 页 · {side_text} · 原第 {getattr(page, 'page_no', 0)} 页")
            self.page_list.addItem(item)
        self.page_list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self.page_list, 1)

        # 预览图
        self.preview_label = QLabel("（无预览图）")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(200)
        self.preview_label.setStyleSheet(
            f"color: {COLORS['text_muted']}; border: 1px solid {COLORS['border']};"
            f"border-radius: {RADIUS['md']}px; background: {COLORS['surface']};")
        layout.addWidget(self.preview_label)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(SPACING["sm"])
        btn_layout.addStretch(1)

        self.btn_cancel = AnimatedButton("取消", button_type="secondary")
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_save = AnimatedButton("保存并结束", button_type="primary")
        self.btn_save.clicked.connect(self.accept)
        btn_layout.addWidget(self.btn_save)

        layout.addLayout(btn_layout)

        if self._pages:
            self.page_list.setCurrentRow(0)

    def _on_row_changed(self, row):
        """选中某一页 ⇒ 显示它的缩略图"""
        if row < 0 or row >= len(self._pages):
            return
        path = getattr(self._pages[row], "cache_path", "")
        pixmap = _load_thumbnail_pixmap(path) if path else None
        if pixmap is not None and not pixmap.isNull():
            self.preview_label.setPixmap(pixmap)
        else:
            self.preview_label.setText("（无预览图）")


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
        # ★ 批次 4：倾斜强提醒（协调器在**工作线程**发 ⇒ GUI 侧走 _post_ui 投递回主线程）
        try:
            self.coordinator.skew_warning.connect(self._on_skew_warning)
        except Exception as e:            # pragma: no cover - 旧版协调器没这个信号
            logger.debug("协调器没有 skew_warning 信号: %s", e)
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
        # WIA 路径的取消信号（GUI 兜底持有；正常已登记进协调器）
        self._wia_cancel_event = None
        # ★ 复审 A1-R1：在途保存用**计数**登记（不是单个布尔标志）
        #   ⇒ 几个在跑就减几个；"全部完成"只在计数归零时成立。
        self._save_tracker = _SaveTracker()
        # 扫描进行中（扫描按钮禁用由它统一裁决）
        self._scan_in_progress = False
        # ★ 复审 A1-R2：关窗等待落盘期间置 True ⇒ 不弹模态框（会卡住关闭）
        self._closing = False

        # ── 文档会话（批次 4 · B4-6）──
        # ★ 必须在 _setup_ui() 之前建好：界面构建时会连接模式切换信号，
        #   信号一触发就会读这些状态。
        self._session_store = document_session.SessionStore()
        self._session = None                   # 当前 DocumentSession（None = 无会话）
        self._session_mode = SESSION_MODE_SINGLE
        self._pending_side_by_job = {}         # job_id → 本批次的页面来源（front/back/single）
        self._pending_resume_from = {}         # job_id → 「从第 N 页重扫」的 N
        self._force_keep_pages = set()          # (job_id, page_no) ⇒ 强制保存（不纠偏）
        self._skew_suppressed_jobs = set()      # 本批次不再提醒倾斜
        self._session_save_finished = False     # 会话落盘完成标记（测试等待用）

        # 先构建界面（隐藏状态）
        self._setup_ui()
        self._refresh_extra_dirs()
        self._update_session_ui()

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
        # ★ 批次 4：启动时恢复未完成的文档会话（并**重新登记缓存保护**）
        self._restore_open_sessions()
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

        # ── 批次 4：扫描模式 / 双面子选项 / 会话状态 ──
        # ★ **追加**在参数区末尾（行 6 起）：既有控件（行 0~5）的位置、
        #   样式、顺序**一律不动**。
        # 扫描模式（单选）
        params_layout.addWidget(QLabel("扫描模式:"), 6, 0)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(SPACING["sm"])
        self.mode_single_radio = QRadioButton("单面")
        self.mode_duplex_radio = QRadioButton("双面（手动）")
        self.mode_batch_radio = QRadioButton("加入现有文档（续扫）")
        for radio in (self.mode_single_radio, self.mode_duplex_radio,
                      self.mode_batch_radio):
            radio.setStyleSheet(_RADIO_QSS)
            mode_row.addWidget(radio)
        mode_row.addStretch(1)
        params_layout.addLayout(mode_row, 6, 1)
        self.mode_single_radio.setChecked(True)
        # ★ 连 `clicked`（不是 `toggled`）：toggled 在互斥切换的**中间态**就发
        #   （旧的先 uncheck、新的后 check）⇒ 用 toggled 会读到错误的模式。
        #   clicked 在状态最终确定后才发 ⇒ 读到的模式一定是最终值。
        for radio in (self.mode_single_radio, self.mode_duplex_radio,
                      self.mode_batch_radio):
            radio.clicked.connect(self._on_scan_mode_changed)

        # 双面子选项
        self.reverse_back_check = QCheckBox("背面顺序反转（双面：背面整叠翻面 ⇒ 顺序相反）")
        self.reverse_back_check.setChecked(True)
        self.reverse_back_check.toggled.connect(self._on_reverse_back_changed)
        params_layout.addWidget(self.reverse_back_check, 7, 0, 1, 2)

        # 会话状态区（只读文本 + 少量按钮）
        self.session_info_label = QLabel("当前会话：无")
        self.session_info_label.setWordWrap(True)
        self.session_info_label.setStyleSheet(f"color: {COLORS['text_secondary']};")
        params_layout.addWidget(self.session_info_label, 8, 0, 1, 2)

        session_btn_layout = QHBoxLayout()
        session_btn_layout.setSpacing(SPACING["sm"])
        self.session_new_btn = AnimatedButton("新建会话", button_type="secondary")
        self.session_new_btn.clicked.connect(self._new_session)
        session_btn_layout.addWidget(self.session_new_btn)

        self.session_save_btn = AnimatedButton("结束并保存", button_type="primary")
        self.session_save_btn.clicked.connect(self._save_session_action)
        session_btn_layout.addWidget(self.session_save_btn)

        self.session_abandon_btn = AnimatedButton("放弃会话", button_type="secondary")
        self.session_abandon_btn.clicked.connect(self._abandon_session_action)
        session_btn_layout.addWidget(self.session_abandon_btn)
        params_layout.addLayout(session_btn_layout, 9, 0, 1, 2)

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
    def _save_cfg(self, cfg=None, what="设置") -> bool:
        """
        写配置并**如实报告**结果（V1-N-2）。

        `scan_coordinator.save_config` 现在在拿不到跨进程锁 / 写失败时返回
        **False**（本次没存上）⇒ GUI 绝不能无条件说"已保存"。
        失败时在状态栏如实说明"未生效"，并记 error 日志。

        返回是否真的落盘。
        """
        target = self.cfg if cfg is None else cfg
        try:
            ok = bool(save_config(target))
        except Exception as e:
            logger.error("保存配置失败: %s", e, exc_info=True)
            ok = False
        if not ok:
            logger.error("配置未落盘（拿不到跨进程锁或写失败）: %s", what)
            try:
                self.status_bar.showMessage(f"{what}保存失败（设置未生效）")
            except Exception:
                pass
        return ok

    def _on_resolution_changed(self, text):
        self.resolution_val = int(text)
        self.cfg["resolution"] = self.resolution_val
        self._save_cfg()

    def _on_color_mode_changed(self, text):
        self.color_mode_val = text
        self.cfg["color_mode_ui"] = self.color_mode_val
        self._save_cfg()

    def _on_format_changed(self, text):
        self.output_format_val = text
        self.cfg["output_format"] = self.output_format_val
        self._save_cfg()

    def _on_source_changed(self, text):
        self.source_val = text
        self.cfg["source"] = self.source_val
        self._save_cfg()
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
        self._save_cfg()

    def _on_direct_save_changed(self, state):
        self.direct_save_val = bool(state)
        self.cfg["direct_save"] = self.direct_save_val
        self._save_cfg()

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
            self._save_cfg()

    def _add_extra_dir(self):
        p = QFileDialog.getExistingDirectory(self, "选择额外输出目录")
        if p and p not in self.extra_dirs:
            self.extra_dirs.append(p)
            self.cfg["extra_dirs"] = self.extra_dirs
            self._save_cfg()
            self._refresh_extra_dirs()

    def _clear_extra_dirs(self):
        self.extra_dirs.clear()
        self.cfg["extra_dirs"] = self.extra_dirs
        self._save_cfg()
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
            self._save_cfg()
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
                self._save_cfg()

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
                self._apply_custom_name(scanner, new_name)

    def _apply_custom_name(self, scanner, new_name) -> bool:
        """
        改名 + 写配置，并**如实报告**（V1-N-2）。返回是否真的落盘。

        ★ `ScanCoordinator.set_custom_name` 内部把 `save_config` 的返回值吞掉了
          ⇒ 这里自己写一次配置（同一份 cfg、同一把跨进程锁），拿得到真实结果。
          （协调器侧建议让 set_custom_name 直接返回 bool，GUI 即可复用）
        """
        scanner.custom_name = new_name
        nicknames = self.coordinator.cfg.get("nicknames", {})
        nicknames[scanner.ip] = new_name
        self.coordinator.cfg["nicknames"] = nicknames
        ok = self._save_cfg(self.coordinator.cfg, "重命名")
        self._rebuild_cards()
        if ok:
            self.status_bar.showMessage(f"已重命名为: {new_name}")
            return True
        self.status_bar.showMessage(f"重命名未保存（设置未生效）: {new_name}")
        QMessageBox.warning(
            self, "保存失败",
            f"重命名未保存（拿不到文件锁或写入失败）：{new_name}")
        return False

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
        # ★ 复审 A1-R1：落盘在途时**不许**再开一次扫描。
        #   扫描按钮在落盘期间是禁用的（见 `_refresh_scan_button`），
        #   这里只是程序化调用时的兜底。
        if _pending_save_count(self):
            self.status_bar.showMessage("正在保存中，请等保存结束后再扫描")
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

        # ── 批次 4：会话模式（双面 / 续扫）──
        # 先把会话切到"扫描中"，并记下这一批是正面还是背面。
        mode = self._current_session_mode()
        side = "single"
        if mode != SESSION_MODE_SINGLE:
            session = self._ensure_session(mode)
            if mode == SESSION_MODE_DUPLEX:
                if session.state == "awaiting_flip":
                    # 用户已翻面 ⇒ 这一批是**背面**
                    side = "back"
                    self._set_session_state(session, "scanning")
                else:
                    side = "front"
            else:  # batch：每一批都是追加
                if session.state == "paused":
                    self._set_session_state(session, "scanning")
                side = "single"
            try:
                self._session_store.save(session)
            except Exception as e:
                logger.error("会话状态落盘失败: %s", e)

        params = {
            "resolution": self.resolution_val,
            "color_mode_ui": self.color_mode_val,
            "output_format": self.output_format_val,
            "source": self.source_combo.currentText(),
            # ★ 手动双面 = **两趟单面**（正面一趟、背面一趟）⇒ 这一趟必须单面，
            #   合并由 doc_merge 负责，不是扫描仪的双面功能。
            "duplex": False if mode == SESSION_MODE_DUPLEX else self.duplex_val,
            "output_dir": out_dir,
        }

        self.scan_btn.setText("扫描中...")
        self.scan_btn.setEnabled(False)
        self._scan_in_progress = True
        self.status_bar.showMessage("正在扫描...")
        self.scan_progress.setVisible(True)
        self.scan_progress.setRange(0, 0)

        self.cancel_scan_btn = AnimatedButton("取消扫描", button_type="accent")
        self.cancel_scan_btn.clicked.connect(self._cancel_scan)
        self.status_bar.addPermanentWidget(self.cancel_scan_btn)

        self._current_job_id = self.coordinator.start_scan(self.selected, params)
        if mode != SESSION_MODE_SINGLE:
            self._pending_side_by_job[self._current_job_id] = side

    def _cancel_scan(self):
        """取消扫描"""
        job_id = getattr(self, "_current_job_id", None)
        if job_id:
            self.coordinator.cancel_scan(job_id)
        # WIA 路径兜底：协调器里没登记成功时，GUI 直接 set **同一个** event
        event = getattr(self, "_wia_cancel_event", None)
        if event is not None:
            event.set()
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
        # ★ 批次 4：会话模式（双面 / 续扫）⇒ **不弹预览对话框**，
        #   页面先进会话，合并后统一给预览。
        #   ★ 取模式必须**防御式**：既有测试里的最小替身（_StubApp）只借了
        #     `_on_scan_finished` 这一个方法，没有会话接线 ⇒ 回落到单面。
        mode = SESSION_MODE_SINGLE
        getter = getattr(self, "_current_session_mode", None)
        if getter is not None:
            try:
                mode = getter()
            except Exception as e:            # pragma: no cover - 防御
                logger.debug("取扫描模式失败，按单面处理: %s", e)
                mode = SESSION_MODE_SINGLE
        if mode != SESSION_MODE_SINGLE and getattr(self, "_session", None) is not None:
            self._session_batch_finished(job_id, result, mode)
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

        ★ 落盘在**后台线程**（16.17 / F-1）：几百页合并 PDF 要几十秒，
          在主线程做会把 Qt 事件循环完全卡死（界面不刷新、不能取消）。
          完成/失败都回主线程再提示 —— 与多页预览保存**同一套机制**
          （`_run_in_background` + 模块级投递器）。
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
        # ★ 复审 A1-R1 ③：输出路径必须**唯一** —— 协调器的文件名精度只到秒
        #   （scan_coordinator.py:727）⇒ 同秒完成的两批算出同一个文件名 ⇒
        #   后写的覆盖先写的。协调器不在本文件范围内 ⇒ GUI 侧兜底。
        output_path = _unique_output_path(result.output_path)
        # ★ 复审 A1-R1 ②：在途保存用**计数**登记（不是单个布尔标志）
        #   ⇒ 几个在跑就减几个；"全部完成"只在计数归零时才成立。
        saves = _save_tracker_of(self)
        saves.begin("direct")
        self._direct_save_finished = False
        # ★ 复审 A1-R1 ①：落盘在途 ⇒ 扫描按钮保持禁用（不许再点一次扫描）
        _refresh_scan_button(self)
        try:
            self.status_bar.showMessage(f"正在保存到 {output_path} …")
        except Exception:       # pragma: no cover - 替身没有状态栏
            pass

        def _work():
            return _save_page_specs(
                result.job_id, result.ext, output_path, page_specs, groups,
                extra_dirs=self.extra_dirs or [], device_name=dev_name,
                device_ip=dev_ip, exposure=exposure)

        # 投递器必须在主线程创建（跨线程 emit 的接收端必须活在主线程）
        _ensure_ui_dispatcher()
        _run_in_background(
            _work,
            lambda files: self._on_direct_save_done(
                files, result, total_pages, exposure),
            lambda err: self._on_direct_save_error(err, result),
            label="直接保存",
        )

    def _on_direct_save_done(self, saved_files, result, total_pages, exposure):
        """主线程：直接保存完成（成功/部分失败都走这里）。"""
        try:
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
            # ★ 关窗等待期间不弹模态框（弹窗会卡住关闭流程）
            if not getattr(self, "_closing", False):
                QMessageBox.information(
                    self, "保存完成" if not failed_pages else "保存部分失败", summary)
        except RuntimeError:
            # 主窗口已被销毁（用户提前关窗）—— 不用再提示
            logger.debug("直接保存完成时主窗口已销毁")
        finally:
            # ★ 完成标志由**计数器**派生：还有别的保存在跑 ⇒ 仍是 False
            #   （复审 A1-R1 ②：单个布尔标志会被"先结束的那次"提前置 True）
            saves = _save_tracker_of(self)
            saves.end("direct")
            self._direct_save_finished = saves.all_done("direct")
            _refresh_scan_button(self)

    def _on_direct_save_error(self, err, result):
        """主线程：直接保存失败 —— 必须让用户看见，不能静默。"""
        try:
            logger.error("直接保存失败: %s", err, exc_info=True)
            self.status_bar.showMessage("直接保存失败")
            if not getattr(self, "_closing", False):
                QMessageBox.critical(self, "保存失败", f"直接保存失败: {err}")
        except RuntimeError:
            logger.debug("直接保存失败时主窗口已销毁")
        finally:
            _release_job_protection(result.job_id)
            # ★ 同上：完成标志由计数器派生
            saves = _save_tracker_of(self)
            saves.end("direct")
            self._direct_save_finished = saves.all_done("direct")
            _refresh_scan_button(self)

    def _release_job(self, job_id):
        """解除 job 的缓存保护（幂等）—— 见模块级 _release_job_protection。"""
        _release_job_protection(job_id)


    def _on_scan_failed(self, job_id, error):
        self._reset_scan_ui()
        QMessageBox.critical(self, "扫描错误", error)
        self.status_bar.showMessage(f"扫描失败: {error[:50]}")

    def _on_scan_cancelled(self, job_id):
        self._reset_scan_ui()
        # ★ 防御式取状态：既有测试的最小替身可能没有会话接线
        pending = getattr(self, "_pending_resume_from", None) or {}
        sides = getattr(self, "_pending_side_by_job", None) or {}
        resume_from = pending.pop(job_id, None)
        side = sides.pop(job_id, "single")
        if resume_from is not None:
            # ★ 「从第 N 页重扫」：把已扫到的页收进会话，再裁掉第 N 页及之后
            self._absorb_cancelled_pages(job_id, side, resume_from)
            self.status_bar.showMessage(
                f"已中断：从第 {resume_from} 页重扫（前 {max(resume_from - 1, 0)} 页已保留）")
            return
        self.status_bar.showMessage("扫描已取消")

    def _reset_scan_ui(self):
        """重置扫描 UI"""
        # ★ 落盘在途时**不许**重新启用扫描按钮（复审 A1-R1）：
        #   后台化之后这里原本无条件 setEnabled(True) ⇒ 用户能在落盘途中再扫一份
        #   ⇒ 两个 save_pages 并发重叠。改成"落盘在途 ⇒ 保持禁用"。
        self._scan_in_progress = False
        _refresh_scan_button(self)
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

    # ────────── 文档会话（批次 4 · B4-6）──────────
    #
    # 三种模式：
    #   单面    → 走既有流程（预览 / 直接保存），**不用会话**
    #   双面    → 会话（front / back 两批）⇒ 交错合并成一个文档
    #   续扫    → 会话（多批追加）⇒ 拼接合并成一个文档
    #
    # ★ 状态与缓存保护的对应（见报告）：
    #   扫完一批   → paused / awaiting_flip ⇒ **保持保护**
    #   落盘成功   → completed ⇒ 解除保护 + 清缓存 + 清页元数据
    #   用户放弃   → abandoned ⇒ 解除保护 + 清缓存
    #   进程重启   → 从 sessions/*.json 恢复 ⇒ **重新 protect_dir**

    # ── 模式 ──
    def _current_session_mode(self) -> str:
        """当前界面选中的扫描模式（single / duplex / batch）"""
        radio = getattr(self, "mode_batch_radio", None)
        if radio is not None and radio.isChecked():
            return SESSION_MODE_BATCH
        radio = getattr(self, "mode_duplex_radio", None)
        if radio is not None and radio.isChecked():
            return SESSION_MODE_DUPLEX
        return SESSION_MODE_SINGLE

    def _reverse_back_enabled(self) -> bool:
        """双面：背面是否整叠翻面（顺序相反）"""
        check = getattr(self, "reverse_back_check", None)
        if check is None:
            return True
        return bool(check.isChecked())

    def _on_reverse_back_changed(self, checked):
        """双面「背面顺序反转」切换 ⇒ 同步到当前会话"""
        session = getattr(self, "_session", None)
        if session is None or session.mode != SESSION_MODE_DUPLEX:
            return
        session.reverse_back = bool(checked)
        try:
            self._session_store.save(session)
        except Exception as e:
            logger.error("会话落盘失败: %s", e)

    def _on_scan_mode_changed(self, _checked=False):
        """
        扫描模式切换。

        ★ 中途从「双面」改选「单面」⇒ **放弃暂存的正面**（brief §3.2）。
          （双面 → 续扫 切换**不**放弃：那些页是有价值的，留着下次启动可恢复。）
        """
        mode = self._current_session_mode()
        session = getattr(self, "_session", None)
        if mode == SESSION_MODE_SINGLE and session is not None:
            if session.mode == SESSION_MODE_DUPLEX and session.total_pages > 0:
                self._discard_session(session, "已放弃暂存的正面（改选单面）")
        self._session_mode = mode
        self._update_session_ui()

    # ── 会话生命周期 ──
    def _ensure_session(self, mode):
        """确保有一个与 mode 匹配、仍打开的会话（没有就新建）"""
        session = getattr(self, "_session", None)
        if session is not None and session.mode == mode and session.is_open:
            return session
        session = document_session.new_session(
            mode=mode,
            ext=self.output_format_val,
            dpi=int(self.resolution_val),
            reverse_back=self._reverse_back_enabled(),
        )
        self._session = session
        try:
            self._session_store.save(session)
        except Exception as e:
            logger.error("新建会话落盘失败: %s", e)
        self._update_session_ui()
        return session

    def _set_session_state(self, session, state):
        """切会话状态（非法转换**不抛** —— 只在确实能切时才切）"""
        if session is None or session.state == state:
            return
        if not session.can_transition(state):
            logger.warning("非法会话状态转换被忽略: %s → %s", session.state, state)
            return
        session.set_state(state)

    def _discard_session(self, session, reason=""):
        """放弃会话：置 abandoned + 解除保护 + 清缓存"""
        if session is None:
            return
        try:
            if session.is_open:
                session.mark_abandoned()
        except Exception as e:
            logger.error("会话置 abandoned 失败: %s", e)
        try:
            self._session_store.save(session)      # 终态 ⇒ 自动解除保护
        except Exception as e:
            logger.error("会话落盘失败: %s", e)
        cleanup_session_jobs(session)
        if getattr(self, "_session", None) is session:
            self._session = None
        if reason:
            self.status_bar.showMessage(reason)
        self._update_session_ui()

    def _new_session(self):
        """「新建会话」"""
        session = getattr(self, "_session", None)
        if session is not None and session.total_pages > 0 and session.is_open:
            reply = QMessageBox.question(
                self, "新建会话",
                f"当前会话还有 {session.total_pages} 页未保存，新建会话将放弃它们。继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            self._discard_session(session, "已放弃当前会话")
        self._session = None
        mode = self._current_session_mode()
        if mode == SESSION_MODE_SINGLE:
            self.status_bar.showMessage("单面模式不使用会话")
            self._update_session_ui()
            return
        self._ensure_session(mode)
        self.status_bar.showMessage("已新建会话")

    def _save_session_action(self):
        """「结束并保存」⇒ 合并预览 → 落盘"""
        session = getattr(self, "_session", None)
        if session is None or session.total_pages == 0:
            QMessageBox.information(self, "提示", "当前没有可保存的会话")
            return
        self._confirm_and_save_session(
            session, duplex=(session.mode == SESSION_MODE_DUPLEX))

    def _abandon_session_action(self):
        """「放弃会话」⇒ 置 abandoned + 解除保护 + 清缓存"""
        session = getattr(self, "_session", None)
        if session is None:
            QMessageBox.information(self, "提示", "当前没有会话")
            return
        reply = QMessageBox.question(
            self, "放弃会话",
            f"放弃当前会话（{session.total_pages} 页）？页面缓存会被清除，无法恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self._discard_session(session, "已放弃会话")

    def _update_session_ui(self):
        """刷新会话状态区的文字与按钮可用性"""
        session = getattr(self, "_session", None)
        label = getattr(self, "session_info_label", None)
        if label is not None:
            if session is None:
                label.setText("当前会话：无")
            else:
                batches = len(set(session.referenced_job_ids))
                mode_text = SESSION_MODE_TEXT.get(session.mode, session.mode)
                state_text = SESSION_STATE_TEXT.get(session.state, session.state)
                label.setText(
                    f"当前会话：{mode_text} / {session.total_pages} 页 / "
                    f"{batches} 批 / {state_text}")
        save_btn = getattr(self, "session_save_btn", None)
        if save_btn is not None:
            save_btn.setEnabled(session is not None and session.total_pages > 0)
        abandon_btn = getattr(self, "session_abandon_btn", None)
        if abandon_btn is not None:
            abandon_btn.setEnabled(session is not None and session.is_open)

    # ── 一批扫描完成 ⇒ 并入会话 ──
    def _session_batch_finished(self, job_id, result, mode):
        """一批扫描完成 ⇒ 并入会话（双面 ⇒ 等翻面 / 批次 ⇒ 暂停）"""
        session = self._session
        if session is None:
            return
        side = self._pending_side_by_job.pop(job_id, "single")
        paths = list(result.cached_paths)
        page_nos = [_page_no_from_path(p) for p in paths]

        # ★ 「从第 N 页重扫」：这一批里第 N 页及之后的页不要
        resume_from = self._pending_resume_from.pop(job_id, None)
        if resume_from is not None:
            keep = [i for i, n in enumerate(page_nos) if n < resume_from]
            for i, n in enumerate(page_nos):
                if n >= resume_from:
                    try:
                        os.remove(paths[i])
                    except OSError as e:
                        logger.debug("删除被丢弃的缓存页失败 [%s]: %s", paths[i], e)
            paths = [paths[i] for i in keep]
            page_nos = [page_nos[i] for i in keep]

        if not paths:
            self.status_bar.showMessage("本批没有可加入会话的页面")
            return

        refs = session.add_pages(job_id, paths, side=side, page_nos=page_nos)
        for ref in refs:
            if (job_id, ref.page_no) in self._force_keep_pages:
                ref.force_keep = True
        session.output_path = result.output_path
        session.ext = result.ext or session.ext

        if mode == SESSION_MODE_DUPLEX and side == "front":
            # 扫完正面 ⇒ 等用户翻面（**不弹预览对话框**）
            self._set_session_state(session, "awaiting_flip")
            try:
                self._session_store.save(session)
            except Exception as e:
                logger.error("会话落盘失败: %s", e)
            self._update_session_ui()
            self.status_bar.showMessage(
                f"正面已扫 {len(refs)} 页，请翻面放回后再次点击「扫描」"
                f"（会话共 {session.total_pages} 页）")
            return

        if mode == SESSION_MODE_DUPLEX and side == "back":
            # 背面也扫完了 ⇒ 合并预览 ⇒ 落盘
            try:
                self._session_store.save(session)
            except Exception as e:
                logger.error("会话落盘失败: %s", e)
            self._update_session_ui()
            self._confirm_and_save_session(session, duplex=True)
            return

        # 批次模式：扫完一批 ⇒ paused（可继续扫描或结束并保存）
        self._set_session_state(session, "paused")
        try:
            self._session_store.save(session)
        except Exception as e:
            logger.error("会话落盘失败: %s", e)
        self._update_session_ui()
        self.status_bar.showMessage(
            f"本批 {len(refs)} 页已加入会话（共 {session.total_pages} 页），"
            f"可继续扫描或结束并保存")

    def _absorb_cancelled_pages(self, job_id, side, resume_from):
        """扫描被中断 ⇒ 把已扫到的页收进会话，再按「从第 N 页重扫」裁掉尾部"""
        session = getattr(self, "_session", None)
        if session is None:
            return
        try:
            pages = cache_manager.list_pages(job_id)
        except Exception as e:
            logger.debug("读取中断扫描的缓存页失败 [%s]: %s", job_id, e)
            return
        if not pages:
            return
        paths = [p for (_n, _ext, p) in pages]
        nos = [n for (n, _ext, _p) in pages]
        refs = session.add_pages(job_id, paths, side=side, page_nos=nos)
        for ref in refs:
            if (job_id, ref.page_no) in self._force_keep_pages:
                ref.force_keep = True
        # ★ 丢弃第 N 页及之后（会话 + 缓存文件），保留前 N-1 页
        resume_from_page(session, job_id, resume_from, drop_cache=True)
        try:
            self._session_store.save(session)
        except Exception as e:
            logger.error("会话落盘失败: %s", e)
        self._update_session_ui()

    # ── 合并 + 落盘 ──
    def _confirm_and_save_session(self, session, duplex=False):
        """合并预览 ⇒ 用户确认 ⇒ 落盘"""
        merged = session_merged_pages(session)
        if not merged:
            QMessageBox.warning(self, "提示", "会话里没有可保存的页面")
            return
        # ★ parent 用 None（不挂到任何可能先销毁的对象上）
        dialog = SessionMergePreviewDialog(session, merged, duplex=duplex)
        accepted = dialog.exec() == QDialog.Accepted
        if not accepted:
            if duplex:
                # 取消合并 ⇒ 回到"待翻面"，页面仍留在会话里
                self._set_session_state(session, "awaiting_flip")
                try:
                    self._session_store.save(session)
                except Exception as e:
                    logger.error("会话落盘失败: %s", e)
            self.status_bar.showMessage("已取消保存；页面仍保留在会话中")
            self._update_session_ui()
            return
        self._save_session_now(session)

    def _default_session_output_path(self) -> str:
        """会话落盘的默认输出路径（与会话里记录的输出路径同规则）"""
        out_dir = self.dir_entry.text() or self.output_dir
        ext = self.output_format_val or "pdf"
        return os.path.join(
            out_dir, f"HP_Scan_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.{ext}")

    def _save_session_now(self, session):
        """会话落盘（**后台线程**：几百页合并 PDF 要几十秒）"""
        scanner = self.selected
        dev_name = (scanner.model or "") if scanner else ""
        dev_ip = (scanner.ip or "") if scanner else ""
        # ★ 输出路径唯一化（复审 A1-R1 ③）：默认路径精度只到秒 ⇒ 同秒的两批会撞名
        out_path = _unique_output_path(
            session.output_path or self._default_session_output_path())
        extra_dirs = list(self.extra_dirs or [])
        session.ext = self.output_format_val or session.ext
        session.dpi = int(self.resolution_val or session.dpi)
        self._session_save_finished = False
        # ★ 在途保存登记（计数）⇒ 落盘期间扫描按钮保持禁用
        saves = _save_tracker_of(self)
        saves.begin("session")
        _refresh_scan_button(self)
        _ensure_ui_dispatcher()

        def _work():
            return save_manager.save_session(
                session, out_path=out_path, extra_dirs=extra_dirs,
                device_name=dev_name, device_ip=dev_ip)

        _run_in_background(_work, self._on_session_saved,
                           self._on_session_save_error, label="会话落盘")

    def _on_session_saved(self, saved_files):
        """主线程：会话落盘完成"""
        session = getattr(self, "_session", None)
        failed = int(getattr(saved_files, "failed_pages", 0) or 0)
        warnings = list(getattr(saved_files, "warnings", []) or [])
        total = session.total_pages if session is not None else len(saved_files)
        try:
            if session is not None:
                if failed:
                    # 部分页失败 ⇒ 缓存保留、会话留在 paused（可重试）
                    self._set_session_state(session, "paused")
                else:
                    self._set_session_state(session, "completed")
                    cleanup_session_jobs(session)
                    self._session = None
                try:
                    self._session_store.save(session)
                except Exception as e:
                    logger.error("会话状态落盘失败: %s", e)
            summary = _format_save_summary(saved_files, total, failed, warnings)
            self.status_bar.showMessage(f"会话已保存：{len(saved_files)} 个文件")
            # ★ 关窗等待期间不弹模态框（弹窗会卡住关闭流程）
            if not getattr(self, "_closing", False):
                QMessageBox.information(
                    self, "保存完成" if not failed else "保存部分失败", summary)
        except RuntimeError:
            logger.debug("会话保存完成时主窗口已销毁")
        finally:
            self._session_save_finished = True
            saves = _save_tracker_of(self)
            saves.end("session")
            _refresh_scan_button(self)
            self._update_session_ui()

    def _on_session_save_error(self, err):
        """主线程：会话落盘失败（会话保留，可重试）"""
        try:
            logger.error("会话落盘失败: %s", err, exc_info=True)
            self.status_bar.showMessage("会话保存失败")
            # ★ 关窗等待期间不弹模态框（弹窗会卡住关闭流程）
            if not getattr(self, "_closing", False):
                QMessageBox.critical(self, "保存失败", f"会话保存失败: {err}")
        except RuntimeError:
            logger.debug("会话保存失败时主窗口已销毁")
        finally:
            self._session_save_finished = True
            saves = _save_tracker_of(self)
            saves.end("session")
            _refresh_scan_button(self)
            self._update_session_ui()

    # ── 跨进程恢复 ──
    def _restore_open_sessions(self):
        """
        启动时恢复未完成的会话。

        ★★ **保护集合是进程内的** ⇒ `cache_manager._protected_dirs` 进程重启后为空
          ⇒ 必须**重新 protect_dir**，否则跨天续扫的页会被 `cleanup_stale`
          （24 小时过期线）删掉。
        """
        try:
            sessions = self._session_store.list_open_sessions()
        except Exception as e:
            logger.error("读取未完成会话失败: %s", e)
            return
        if not sessions:
            return
        # 先把**所有**未完成会话引用的 job 目录重新登记保护
        # （即使用户选择不继续，页面也不该被自动清理删掉）
        for session in sessions:
            protect_session_jobs(session)
        latest = sessions[-1]
        reply = QMessageBox.question(
            self, "未完成的文档会话",
            f"有未完成的文档会话（已扫 {latest.total_pages} 页），是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply == QMessageBox.Yes:
            self._adopt_session(latest)
        else:
            self.status_bar.showMessage(f"有 {len(sessions)} 个未完成会话（未继续）")

    def _adopt_session(self, session):
        """接管一个未完成的会话（并把界面切到对应模式）"""
        self._session = session
        mode = session.mode
        radio = None
        if mode == SESSION_MODE_DUPLEX:
            radio = getattr(self, "mode_duplex_radio", None)
        elif mode == SESSION_MODE_BATCH:
            radio = getattr(self, "mode_batch_radio", None)
        else:
            radio = getattr(self, "mode_single_radio", None)
        if radio is not None:
            radio.setChecked(True)          # ★ 用 setChecked（不触发 clicked）
        check = getattr(self, "reverse_back_check", None)
        if check is not None:
            check.setChecked(bool(session.reverse_back))
        protect_session_jobs(session)
        self._update_session_ui()
        self.status_bar.showMessage(f"已恢复会话（{session.total_pages} 页）")

    # ────────── 倾斜强提醒（批次 4）──────────
    def _on_skew_warning(self, job_id, page_no, angle, cache_path):
        """
        协调器在**工作线程**发来的倾斜强提醒。

        ★ 绝不在工作线程里弹窗（Qt 要求 GUI 操作在主线程）⇒ 走 `_post_ui`
          投递回主线程（这是本文件既有的跨线程通道）。
        """
        try:
            self._post_ui(
                lambda: self._show_skew_warning(job_id, page_no, angle, cache_path))
        except RuntimeError:
            logger.debug("倾斜提醒投递失败（主窗口已销毁）")

    def _show_skew_warning(self, job_id, page_no, angle, cache_path):
        """主线程：弹倾斜强提醒（三个按钮）"""
        if job_id in self._skew_suppressed_jobs:
            return
        try:
            # ★★ parent 用 None：绝不把弹窗挂到可能先销毁的对象上
            #   （上一轮 0xc0000374 堆损坏的根因）
            dialog = SkewWarningDialog(job_id, page_no, angle, cache_path)
            dialog.exec()                    # ★ 先弹窗
            choice = dialog.choice            # ★ 结果用属性取（不是信号）
            dialog.deleteLater()             # ★ 之后不再碰这个对象
        except RuntimeError as e:
            logger.debug("倾斜提醒弹窗失败: %s", e)
            return
        if choice == SkewWarningDialog.CHOICE_RESUME:
            self._resume_from_page(job_id, page_no)
        elif choice == SkewWarningDialog.CHOICE_FORCE:
            self._force_keep_pages.add((job_id, int(page_no)))
            self._skew_suppressed_jobs.add(job_id)
            self.status_bar.showMessage(f"第 {page_no} 页将原样保存（不纠偏）")
        else:
            self._skew_suppressed_jobs.add(job_id)
            self.status_bar.showMessage("继续扫描（自动纠偏）")

    def _resume_from_page(self, job_id, page_no):
        """
        「从第 N 页重扫」—— 中断当前扫描 + 丢弃第 N 页及之后的页（保留前 N-1 页）。

        ★ 可复用 API：纯函数版本见模块级 `resume_from_page(session, job_id, page_no)`。
        """
        page_no = int(page_no)
        try:
            self.coordinator.cancel_scan(job_id)
        except Exception as e:
            logger.debug("取消扫描失败 [%s]: %s", job_id, e)
        # 记下来：扫描被中断后（_on_scan_cancelled）按 N 裁掉尾部。
        # ★ 只有**这一批还在扫描中**时才需要记 —— 已经并入会话的批次
        #   直接在下面裁掉即可（否则会重复收录）。
        pending_sides = getattr(self, "_pending_side_by_job", None) or {}
        if job_id in pending_sides:
            self._pending_resume_from[job_id] = page_no
        session = getattr(self, "_session", None)
        if session is not None:
            dropped = resume_from_page(session, job_id, page_no, drop_cache=True)
            if dropped:
                logger.info("从第 %d 页重扫：丢弃 %d 页", page_no, len(dropped))
                # ★ 立刻落盘：删页必须**活过落盘**（SessionStore.save 默认
                #   merge=True 只进不退 ⇒ remove_pages_from 置位的 full_rewrite
                #   标志会让 save 改走整体覆盖 ⇒ 磁盘上的副本也不会把页复活）。
                try:
                    self._session_store.save(session)
                except Exception as e:
                    logger.error("会话落盘失败: %s", e)
        self.status_bar.showMessage(f"已中断扫描，将从第 {page_no} 页重扫")

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
                        output_path = os.path.join(out_dir, f"HP_Listen_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.{ext}")
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
                        output_path = os.path.join(out_dir, f"HP_Listen_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.{ext}")
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
                self._save_cfg()
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
            from wia_engine import list_wia_scanners
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
            self._start_wia_scan(device)

        scan_btn.clicked.connect(_do_wia_scan)
        dialog.exec()

    def _start_wia_scan(self, device):
        """
        启动一次 WIA 扫描（独立成方法 ⇒ 可单独测试，不依赖设备选择对话框）。

        ★ 取消能力（V3-F-4）：wia_engine 已支持 `cancel_event`（取消时抛
          `ScanCancelled`），这里把**协调器登记的那个 event**透传给引擎，
          并给出与 eSCL 路径一致的「取消扫描」入口
          （取消按钮 → `coordinator.cancel_scan(job_id)`）。
        ★ 取消后走 `scan_cancelled` 语义（**不是** `scan_failed`）。
        """
        try:
            from wia_engine import wia_scan
        except ImportError:
            QMessageBox.warning(self, "提示", "WIA 引擎不可用（仅 Windows 支持）")
            return

        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        cancel_event = threading.Event()
        # 登记到协调器 ⇒ coordinator.cancel_scan(job_id) 停的是**同一个** event
        self._register_cancel_event(job_id, cancel_event)
        self._wia_cancel_event = cancel_event
        self._current_job_id = job_id

        self.status_bar.showMessage("正在通过 WIA 扫描...")
        # 新的一次扫描 = 新文档 ⇒ 清掉残留曝光（I-6）
        self._reset_exposure_state()

        # 取消入口：与 eSCL 路径一致（状态栏上的「取消扫描」按钮）
        self.scan_btn.setEnabled(False)
        self._scan_in_progress = True
        self.cancel_scan_btn = AnimatedButton("取消扫描", button_type="accent")
        self.cancel_scan_btn.clicked.connect(self._cancel_scan)
        self.status_bar.addPermanentWidget(self.cancel_scan_btn)

        def _wia_thread():
            try:
                data, ext = wia_scan(
                    device_id=device.get("id"),
                    resolution=self.resolution_val,
                    color_mode=1,
                    output_format=self.output_format_val,
                    cancel_event=cancel_event,
                )
                if data:
                    cache_manager.register_job(job_id)
                    cache_path = cache_manager.write_page(job_id, 1, data, ext)
                    del data
                    out_dir = self.dir_entry.text() or self.output_dir
                    output_path = os.path.join(
                        out_dir,
                        f"HP_WIA_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.{ext}"
                    )
                    self._post_ui(lambda: self._show_preview(
                        cache_path, ext, output_path, job_id))
                else:
                    self._post_ui(lambda: self._reset_scan_ui())
                    self._post_ui(lambda: self.status_bar.showMessage("WIA 扫描失败"))
            except ScanCancelled:
                # ★ 用户取消 ⇒ **不是失败**（V3-F-4）：发 scan_cancelled，
                #   绝不能发 scan_failed（否则用户看到"扫描失败"）。
                self._post_ui(lambda: self._on_wia_cancelled(job_id))
            except Exception as e:
                self._post_ui(lambda: self._scan_error(f"WIA 错误: {e}"))
                # 预览没开起来 ⇒ 没人替我们注销 job（否则缓存目录永不被回收）
                self._release_job(job_id)
            finally:
                self._clear_cancel_event(job_id)
                self._wia_cancel_event = None

        threading.Thread(target=_wia_thread, daemon=True).start()

    def _on_wia_cancelled(self, job_id):
        """WIA 扫描被用户取消（取消 ≠ 失败）。"""
        try:
            self._reset_scan_ui()
            self.status_bar.showMessage("扫描已取消")
            # 发协调器的 scan_cancelled（与 eSCL 路径同一套信号语义）——
            # ★ WIA 路径是 GUI 自己起的线程（协调器没有 WIA 路径）⇒ 这里补发，
            #   保证上层收到的是"取消"而不是"失败"。
            self.coordinator.scan_cancelled.emit(job_id)
        except RuntimeError:
            logger.debug("WIA 取消时主窗口已销毁")

    def _register_cancel_event(self, job_id, event):
        """
        把取消信号登记到协调器（让 `coordinator.cancel_scan(job_id)` 能停掉它）。

        ★ 协调器目前只在 `start_scan` 内部登记它自己那条 eSCL 的 cancel_event，
          没有对外的登记 API；WIA 路径是 GUI 自己起的线程 ⇒ 这里按协调器的
          内部约定登记**同一个** event 对象（引擎收到的就是它）。
        ★ 登记失败不致命：GUI 自己持有 event，取消按钮会直接 set 它。
        """
        try:
            with QMutexLocker(self.coordinator._cancel_mutex):
                self.coordinator._cancel_events[job_id] = event
            return True
        except Exception as e:
            logger.debug("登记取消信号失败（改用 GUI 侧兜底）: %s", e)
            return False

    def _clear_cancel_event(self, job_id):
        """注销协调器里的取消信号（与协调器 _ScanTask 的收尾一致）。"""
        try:
            self.coordinator._clear_cancel_event(job_id)
        except Exception as e:
            logger.debug("注销取消信号失败: %s", e)

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
            ok = self._apply_cache_settings(
                config, limit_spin.value(), trigger_spin.value(),
                cleanup_spin.value())
            dialog.accept()
            return ok

        save_btn.clicked.connect(_save)
        dialog.exec()

    def _apply_cache_settings(self, config, limit_mb, trigger_percent,
                              cleanup_mb) -> bool:
        """
        写缓存设置并**如实报告**（V1-N-2）。返回是否真的落盘。

        `cache_manager.save_cache_config` 在拿不到跨进程锁 / 写失败时返回
        **False**（本次没存上）⇒ 绝不能无条件说"已保存"。
        """
        apply_cache_settings(config, limit_mb, trigger_percent, cleanup_mb)
        ok = bool(cache_manager.save_cache_config(config))
        if ok:
            self.status_bar.showMessage("缓存设置已保存")
            return True
        logger.error("缓存设置未落盘（拿不到跨进程锁或写失败）")
        self.status_bar.showMessage("缓存设置保存失败（设置未生效）")
        QMessageBox.warning(
            self, "保存失败",
            "缓存设置保存失败（拿不到文件锁或写入失败），本次设置未生效。")
        return False

    # ────────── 窗口关闭时保存设置 ──────────

    # 关闭时等待在途落盘的上限（秒）—— **有界** ⇒ 界面不会无限期卡住
    CLOSE_SAVE_WAIT_SECONDS = 30.0

    def _wait_background_tasks(self, tasks, timeout):
        """
        有界等待在途后台任务结束。返回 True = 全部结束。

        ★ 等待期间**继续泵事件**（界面不冻结），但**排除用户输入**
          （用户在点"关闭"了，别让别的点击插进来）。
        ★ 关窗等待期间不弹模态框（弹窗会卡住关闭流程）⇒ 由 `_closing` 统一抑制。
        """
        deadline = time.monotonic() + timeout
        while True:
            if not any(t.is_alive() for t, _lbl in tasks):
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            except Exception as e:        # pragma: no cover - 事件循环异常
                logger.debug("关窗等待时泵事件失败: %s", e)
            time.sleep(0.02)

    def closeEvent(self, event):
        """关闭窗口时保存窗口大小和位置"""
        self.cfg["window_width"] = self.width()
        self.cfg["window_height"] = self.height()
        self.cfg["window_x"] = self.x()
        self.cfg["window_y"] = self.y()
        self._save_cfg()
        # ★ 复审 A1-R2：落盘线程原来是 daemon ⇒ 用户在落盘**中途**关窗 ⇒
        #   进程被直接杀掉 ⇒ 磁盘上留下**截断**的 PDF（能打开、页数却不对）。
        #   这里在关窗前**有界等待**在途后台任务；等不到就**阻止关闭**并
        #   明确告知用户"有未完成的保存"（落盘线程已改成非 daemon ⇒
        #   即便用户强行结束进程，Python 也会 join 它把文件写完）。
        pending = pending_background_tasks()
        if pending:
            self._closing = True
            try:
                finished = self._wait_background_tasks(
                    pending, self.CLOSE_SAVE_WAIT_SECONDS)
            finally:
                self._closing = False
            if not finished:
                names = "、".join(lbl for _t, lbl in pending)
                logger.error("关闭被阻止：仍有后台任务未完成: %s", names)
                try:
                    QMessageBox.warning(
                        self, "保存尚未完成",
                        f"还有 {len(pending)} 个保存任务没有完成（{names}）。\n\n"
                        "已阻止关闭，以免留下写了一半的文件。\n"
                        "请等保存结束后再关闭窗口。")
                except RuntimeError:       # pragma: no cover - 主窗口已销毁
                    pass
                event.ignore()
                return
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
        # 裁边框所属的**原图**尺寸：裁边是在原图坐标系里算的，
        # 写 page_meta 时要按当前旋转把它变换到旋转后的坐标系（F-2）
        self._crop_source_size = None
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
        ★ **不再清零用户的旋转**（F-2）：旋转是用户明确做的操作，裁边不该
          把它悄悄丢掉。裁边在**原图坐标系**里算（与预览里看到的是同一块
          内容：旋转 90° 只会把宽高对调），裁边框写 page_meta 时再按当前
          旋转变换到旋转后的坐标系。
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
            source_size = img.size          # 原图尺寸（裁边框的坐标系）
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
            self._crop_source_size = source_size
            # ★ 旋转**保留**：用户转过的角度不会被裁边悄悄清零
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

        ★ 记的是**最终产物**的几何（裁边 + 旋转之后）：
          - width/height = 旋转后的尺寸（90/270 宽高对调）
          - crop_rect   = 裁边框变换到**旋转后的坐标系**（F-2）
            （裁边框本身是在原图坐标系里算出来的）
        """
        try:
            source = self._source_path()
            with Image.open(source) as im:
                width, height = im.size
            width, height = _rotated_size((width, height), self._rotation)
            crop_rect = self._crop_rect
            if crop_rect is not None:
                src_size = self._crop_source_size
                if not src_size:
                    try:
                        with Image.open(self.cache_path) as im:
                            src_size = im.size
                    except Exception:
                        src_size = (width, height)
                crop_rect = _rotate_rect(crop_rect, self._rotation, src_size)
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
                crop_rect=tuple(crop_rect) if crop_rect else None,
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
        # 裁边框所属的**原图**尺寸：页号 → (W, H)（F-2 坐标变换用）
        self._crop_source_sizes = {}
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
        ★ **不再清掉该页的旋转**（F-2）：旋转是用户明确做的操作，裁边不该
          把它悄悄丢掉。裁边在**原图坐标系**里算，裁边框写 page_meta 时
          再按该页当前旋转变换到旋转后的坐标系。
        返回 True 表示确实裁了。
        """
        source = self._cache_path(page_num)
        with open(source, "rb") as f:
            raw = f.read()
        with Image.open(io.BytesIO(raw)) as src:
            img = src.copy()
        source_size = img.size          # 原图尺寸（裁边框的坐标系）
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
        self._crop_source_sizes[page_num] = source_size
        self._cropped.add(page_num)
        # ★ 旋转**保留**：用户转过的角度不会被裁边悄悄清零
        return True

    def _sync_page_meta(self, page_num):
        """
        把裁边/旋转后的几何信息写回 page_meta（原始件不动，元数据要跟上）。
        （page_meta 只**调用**，不改它的实现）

        ★ 记的是**最终产物**的几何（裁边 + 旋转之后）：
          - width/height = 旋转后的尺寸（90/270 宽高对调）
          - crop_rect   = 裁边框变换到**旋转后的坐标系**（F-2）
        """
        try:
            source = self._source_path(page_num)
            with Image.open(source) as im:
                width, height = im.size
            rotation = self._get_rotation(page_num)
            width, height = _rotated_size((width, height), rotation)
            crop_rect = self._crop_rects.get(page_num)
            if crop_rect is not None:
                src_size = self._crop_source_sizes.get(page_num)
                if not src_size:
                    try:
                        with Image.open(self._cache_path(page_num)) as im:
                            src_size = im.size
                    except Exception:
                        src_size = (width, height)
                crop_rect = _rotate_rect(crop_rect, rotation, src_size)
            store = page_meta.PageMetaStore(cache_manager._cache_root())
            existing = {}
            try:
                existing = store.get_job(self.job_id).get(str(page_num), {}) or {}
            except Exception:
                existing = {}
            store.set_page(self.job_id, page_num, page_meta.PageMeta(
                cache_path=self._cache_path(page_num),
                width=int(width), height=int(height),
                manual_rotation=int(rotation),
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
            self._crop_source_sizes.pop(page_num, None)
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
        ★ 只剩 1 页（且只有 1 组）时走 GUI 的统一入口 `_save_page_specs`
          （F-4）：单页单组**不加序号**（`{base}.{ext}`），与「单页任务」
          /「直接保存」完全一致；多页仍走 save_manager.run_save_job
          （纯函数入口 + PDF 体积预检回调，无 Qt 依赖）。
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

        # ★ 单页单组：走 GUI 的统一入口 `_save_page_specs`（F-4）
        #   —— 命名与「单页任务」/「直接保存」一致：`{base}.{ext}`（不加序号）。
        if len(page_specs) == 1 and len(groups) == 1:
            def _work_single():
                return _save_page_specs(
                    self.job_id, self.ext, self.output_path, page_specs, groups,
                    extra_dirs=self.extra_dirs, device_name=self.device_name,
                    device_ip=self.device_ip, exposure=exposure)

            # 投递器在主线程创建（跨线程 emit 的接收端必须活在主线程）
            _ensure_ui_dispatcher()
            _run_in_background(
                _work_single,
                lambda files: self._on_save_done(files),
                lambda err: self._on_save_error(err),
            )
            return

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
