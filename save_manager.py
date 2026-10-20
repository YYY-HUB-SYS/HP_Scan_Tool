"""
保存落盘管理器 — HP Scan Tool v4.0
把扫描结果写入目标目录：格式转换 / 分组命名 / 额外目录复制 / 缓存清理 / 历史写入。
纯 Python 实现，不依赖 Qt 与任何 GUI，可独立单元测试。

PDF 路径（批次 2）：
- 打印机只出图片，PDF 在本地合成（见 escl_engine.REQUEST_FORMAT）
- 多页 PDF **两阶段**合成，内存特征分两段看：
  * 阶段一（逐页转单页 PDF）：内存里同时只有一页 ⇒ **O(1)**
  * 阶段二（pypdf 逐页 append 合并）：整份 PDF 读进内存 ⇒ **O(产物 PDF 体积)**
    实测峰值 ≈ **2.1 × 最终 PDF 体积**（30 页 A4@300dpi：产物 100.0MB，
    峰值 204.4MB），**随页数线性增长**
  ⇒ 合并前用 `estimate_pdf_peak_bytes` 做体积预检（门禁 1.5GB）
- 单页与多页 PDF 共用 _a4_canvas，页面尺寸必然一致
- 最终 PDF 先写同目录临时文件、成功后再 os.replace（不留半截文件）

自动纠偏（批次 4）：
- 顺序固定为 **曝光 → 手动旋转 → 自动纠偏 → A4 归一**
- 纠偏角来自页元数据（page_meta.deskew_angle，扫描时由 skew_detect 测出）
- 阈值：|angle| >= deskew_apply_threshold（默认 0.5°）才施加；
  角度为 0 时**一个像素都不动** ⇒ 直页产物与改造前逐字节相同
- skew_detect 是**静态 import** ⇒ PyInstaller 依赖分析会自动收录它
"""

import io
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field

from PIL import Image, UnidentifiedImageError

import cache_manager
import config_io
import doc_merge
import history_manager
import page_meta
import skew_detect
from exposure import (DEFAULT_GAMMA, DEFAULT_GAINS, GAMMA_MAX, GAMMA_MIN,
                      PARAM_MAX, PARAM_MIN, apply_exposure, sanitize_gains,
                      sanitize_int, sanitize_number)

try:
    from pypdf import PdfWriter
except ImportError:  # pragma: no cover - 缺依赖时给明确报错，而不是导入就炸
    PdfWriter = None

logger = logging.getLogger(__name__)

__all__ = [
    "build_output_name", "copy_to_extra_dirs", "save_pages", "save_session",
    "save_single",
    "estimate_pdf_peak_bytes", "run_save_job",
    "SaveRequest", "SaveResult", "SaveAborted",
    "PDF_PEAK_FACTOR", "PDF_PEAK_LIMIT_BYTES",
    "DESKEW_CONFIG_DEFAULTS", "resolve_deskew_settings", "load_deskew_settings",
    "deskew_angle_to_apply", "load_job_deskew_angles", "resolve_deskew_angles",
]

# PDF 合成的内存峰值系数：实测阶段二峰值 ≈ 2.1 × 最终 PDF 体积
PDF_PEAK_FACTOR = 2.1
# PDF 合成的内存峰值门禁（与批次 4 门禁一致）：1.5GB
PDF_PEAK_LIMIT_BYTES = int(1.5 * 1024 ** 3)

# ── 自动纠偏策略（批次 4）────────────────────────────────────────────────
# 这四个键写进 scan_config.json（scan_coordinator 用 load_config/save_config 落盘）。
# ★ 这里是**唯一真源**：scan_coordinator 从这里 import，避免两处默认值漂移。
#
# ★★ 两个角度阈值不是一回事，务必分清：
#   · 0.75°（skew_detect._MIN_EFFECTIVE_ANGLE）= **检出**下限 ——
#     estimate_skew 低于它就直接返回 0.0/None ⇒ 这个角度**根本测不出来**，
#     所以它决定"能不能测到"，与"要不要施加"无关。
#   · 0.5°（deskew_apply_threshold）= **施加**下限 ——
#     已经测出来了，但小于它就不做重采样（避免一次无谓的 BICUBIC + 四角补白）。
#   默认 0.5 < 0.75 ⇒ **施加阈值在默认配置下永不生效**：
#   凡是非零的检出角必然 >= 0.75°，一定会被施加。
#   把 deskew_apply_threshold 调高（例如 1.5）才会出现"测得出来但不施加"。
DESKEW_CONFIG_DEFAULTS = {
    "deskew_enabled": True,          # 总开关：关掉 ⇒ 只检测不纠偏、也不提醒
    "deskew_apply_threshold": 0.5,   # 施加纠偏的最小角度（度）
    "skew_warn_threshold": 3.0,      # 强提醒阈值（度）
    "skew_warn_consecutive": 3,      # 连续几页超阈值才提醒
}

# 缓存页文件名里的页号（page_003.jpg / crop_page_003.jpg 都认）
_PAGE_NUM_RE = re.compile(r"page_(\d+)\.", re.IGNORECASE)


def _as_bool(value, default: bool) -> bool:
    """脏值容忍的布尔解析（"false"/"0"/"关" 都认；认不出 ⇒ 用默认值）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "是", "开"):
            return True
        if text in ("0", "false", "no", "off", "否", "关"):
            return False
    return default


def _as_float(value, default: float, minimum: float = 0.0) -> float:
    """脏值容忍的浮点解析（非数/NaN/低于下限 ⇒ 回落默认值）。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:          # NaN
        return default
    if result < minimum:
        return default
    return result


def _as_int(value, default: int, minimum: int = 1) -> int:
    """脏值容忍的整数解析（低于下限 ⇒ 回落默认值）。"""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= minimum else default


def resolve_deskew_settings(cfg=None) -> dict:
    """
    从配置字典解析纠偏阈值（缺省/非法值 ⇒ 回落默认值）。

    纯函数，不碰磁盘 —— 协调器（检测/提醒）与落盘（施加）共用这一份语义。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    defaults = DESKEW_CONFIG_DEFAULTS
    return {
        "deskew_enabled": _as_bool(cfg.get("deskew_enabled"),
                                   defaults["deskew_enabled"]),
        "deskew_apply_threshold": _as_float(
            cfg.get("deskew_apply_threshold"),
            defaults["deskew_apply_threshold"]),
        "skew_warn_threshold": _as_float(
            cfg.get("skew_warn_threshold"),
            defaults["skew_warn_threshold"]),
        "skew_warn_consecutive": _as_int(
            cfg.get("skew_warn_consecutive"),
            defaults["skew_warn_consecutive"]),
    }


def load_deskew_settings() -> dict:
    """
    读 scan_config.json 里的纠偏阈值（文件缺失/损坏 ⇒ 回落默认值）。

    ★ 与 scan_coordinator.load_config 读的是**同一个文件**
      （%APPDATA%\\HP_Scan_Tool\\scan_config.json）：协调器负责写，这里只读。
    ★ 只读不写 ⇒ 落盘路径上不会碰用户配置。
    """
    path = os.path.join(config_io.config_dir(), "scan_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    return resolve_deskew_settings(cfg)


def deskew_angle_to_apply(angle, threshold=None) -> float:
    """
    返回**真正要施加**的纠偏角（0.0 ⇒ 不施加，像素一个都不动）。

    施加阈值 = deskew_apply_threshold（默认 0.5°），与"检出下限 0.75°"是两回事。
    """
    if angle is None:
        return 0.0
    try:
        value = float(angle)
    except (TypeError, ValueError):
        return 0.0
    if value != value:            # NaN
        return 0.0
    if threshold is None:
        threshold = DESKEW_CONFIG_DEFAULTS["deskew_apply_threshold"]
    try:
        limit = float(threshold)
    except (TypeError, ValueError):
        limit = DESKEW_CONFIG_DEFAULTS["deskew_apply_threshold"]
    if limit < 0:
        limit = 0.0
    if abs(value) < limit or value == 0.0:
        return 0.0
    return value


def _norm_path(path) -> str:
    """路径归一（用于比对缓存页路径）"""
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except Exception:
        return str(path)


def _page_no_from_path(path) -> int | None:
    """从缓存页文件名里认页号（page_003.jpg / crop_page_003.jpg 都认）。"""
    match = _PAGE_NUM_RE.search(os.path.basename(str(path)))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def load_job_deskew_angles(job_id: str) -> tuple[dict, dict]:
    """
    读某 job 的页元数据 ⇒ (按页号, 按缓存路径) 的纠偏角表。

    纠偏角是**扫描时**测出来的，写在 page_meta.deskew_angle 里；
    落盘时从这里取回施加角度（原件始终不动）。
    """
    by_page: dict = {}
    by_path: dict = {}
    if not job_id:
        return by_page, by_path
    try:
        store = page_meta.PageMetaStore(cache_manager._cache_root())
        job = store.get_job(job_id) or {}
    except Exception as e:
        logger.debug("读页元数据（纠偏角）失败 [%s]: %s", job_id, e)
        return by_page, by_path
    if not isinstance(job, dict):
        return by_page, by_path
    for key, payload in job.items():
        if not isinstance(payload, dict):
            continue
        try:
            angle = float(payload.get("deskew_angle") or 0.0)
        except (TypeError, ValueError):
            continue
        try:
            page_no = int(key)
        except (TypeError, ValueError):
            continue
        by_page[page_no] = angle
        cache_path = payload.get("cache_path")
        if cache_path:
            by_path[_norm_path(cache_path)] = angle
    return by_page, by_path


def resolve_deskew_angles(job_id: str, page_specs) -> list[float]:
    """
    按页解析纠偏角（返回与 page_specs 等长的角度列表）。

    匹配优先级：
      ① 缓存路径精确匹配（page_meta 记的 cache_path）
      ② 文件名里的页号（page_003.jpg / crop_page_003.jpg）
      ③ page_specs 下标 + 1
    """
    by_page, by_path = load_job_deskew_angles(job_id)
    angles: list[float] = []
    for index, spec in enumerate(page_specs or []):
        cache_file = spec[0] if isinstance(spec, (tuple, list)) else spec
        angle = by_path.get(_norm_path(cache_file), 0.0)
        if not angle:
            page_no = _page_no_from_path(cache_file)
            if page_no is None:
                page_no = index + 1
            angle = by_page.get(page_no, 0.0)
        angles.append(float(angle))
    return angles


class SaveAborted(RuntimeError):
    """用户主动放弃保存（例如体积预检弹窗里选了「取消」）。"""


class SaveResult(list):
    """
    save_pages 的返回值。

    **行为与 list 完全一致**（现有调用方的 `if not saved_files` /
    `saved_files[0]` / 迭代都不受影响），额外携带失败页信息 ——
    让"产物少页"这件事能被调用方看到，而不是只累加一个局部计数。
    """

    def __init__(self, saved_files=(), failed_pages: int = 0,
                 skipped_pages=None, warnings=None, saved_page_count: int = 0):
        super().__init__(saved_files)
        self.failed_pages = int(failed_pages)
        # 被跳过的页在 page_specs 里的下标（0 基）
        self.skipped_pages = list(skipped_pages or [])
        self.warnings = list(warnings or [])
        # 实际写出的**页数**（PDF 一个文件多页；图片一个文件一页）
        self.saved_page_count = int(saved_page_count) if saved_page_count else len(self)

    @property
    def ok(self) -> bool:
        return len(self) > 0


@dataclass
class SaveRequest:
    """
    后台保存任务的一次性入参（供 QThreadPool / QRunnable 使用）。

    只装数据，不带任何 GUI 对象 —— 可以安全地跨线程传递。
    """
    job_id: str
    ext: str
    output_path: str
    page_specs: list
    groups: list
    extra_dirs: list | None = None
    device_name: str = ""
    device_ip: str = ""
    exposure: dict | None = None
    # 体积预检的确认回调：收到提示文案，返回 False 表示用户放弃保存
    confirm: Callable | None = field(default=None)


def run_save_job(req: SaveRequest) -> SaveResult:
    """
    **后台保存入口**（纯函数，无 Qt 依赖，可直接放进 QRunnable 里跑）。

    与 save_pages 等价，只是把入参打包成 SaveRequest，方便跨线程传递。
    返回 SaveResult（list 子类），调用方从 `.failed_pages` /
    `.skipped_pages` / `.warnings` 取失败页与提示。
    """
    return save_pages(req.job_id, req.ext, req.output_path, req.page_specs,
                      req.groups, extra_dirs=req.extra_dirs,
                      device_name=req.device_name, device_ip=req.device_ip,
                      exposure=req.exposure, confirm=req.confirm)


def estimate_pdf_peak_bytes(page_specs) -> int:
    """
    估算 PDF 合成的内存峰值（字节）。

    依据：阶段二峰值 ≈ PDF_PEAK_FACTOR × 产物 PDF 体积；而产物体积与
    **缓存页字节总量**同量级（同一批扫描内容的两种编码），所以用
    "实际读到的缓存页文件大小求和"比猜 DPI 更准。

    估算不出来（文件已被清理 / 读不到）时按 0 计 —— 宁可低估，
    也不要把一次正常保存误判成超限。
    """
    total = 0
    for item in page_specs or []:
        path = item[0] if isinstance(item, (tuple, list)) else item
        try:
            total += os.path.getsize(path)
        except OSError:
            continue
    return int(total * PDF_PEAK_FACTOR)



def build_output_name(base: str, ext: str, group_count: int,
                      group_index: int, in_group_seq: int) -> str:
    """
    生成输出文件名。

    单组（group_count <= 1）—— 保持既有命名不变：
        PDF  -> {base}.pdf
        其他 -> {base}_{组内序号:03d}.{ext}
    多组（group_count > 1）—— 组间不得覆盖：
        PDF  -> {base}_{组号:03d}.pdf
        其他 -> {base}_{组号:03d}_{组内序号:03d}.{ext}
    """
    if ext == "pdf":
        if group_count <= 1:
            return f"{base}.pdf"
        return f"{base}_{group_index:03d}.pdf"
    if group_count <= 1:
        return f"{base}_{in_group_seq:03d}.{ext}"
    return f"{base}_{group_index:03d}_{in_group_seq:03d}.{ext}"


def _int_param(exposure, key: str) -> int:
    """净化层取值：标量参数非法 ⇒ 回落 0；类型完全不对 ⇒ 也回落 0。

    这里是**外部输入边界**（预设 JSON / GUI 回填），什么脏值都可能出现，
    因此这里不抛 —— 引擎层（exposure.apply_exposure）仍然对类型错误抛 TypeError。
    """
    try:
        return sanitize_int(exposure.get(key), 0, PARAM_MIN, PARAM_MAX, key)
    except TypeError:
        return 0


def _gamma_param(exposure) -> float:
    """净化层取值：gamma 非法 ⇒ 回落 1.0（不抛）。"""
    try:
        return sanitize_number(exposure.get("gamma"), DEFAULT_GAMMA,
                               GAMMA_MIN, GAMMA_MAX, "gamma")
    except TypeError:
        return DEFAULT_GAMMA


def _exposure_params(exposure) -> dict | None:
    """
    把曝光参数字典规范化成 apply_exposure 的 kwargs。

    返回 None 表示「曝光不生效」——此时调用方必须走与改造前完全一致的路径，
    保证 exposure=None / mode="off" 时产物逐字节不变（向后兼容）。

    ★ **净化层**（N3）：这一层与引擎层（exposure.apply_exposure）**都要**挡住
    非法值 —— 预设 JSON 由用户编辑，gamma="abc" / gamma=-1 / brightness="x"
    这类脏值不能一路传到引擎，更不能让整次保存失败。
    这里的净化结果同时决定「曝光是否生效」（见 save_pages 的快路径判断）。
    """
    if not exposure:
        return None
    mode = str(exposure.get("mode") or "off").strip().lower()
    if mode not in ("auto", "manual"):
        return None

    return {
        "mode": mode,
        "brightness": _int_param(exposure, "brightness"),
        "contrast": _int_param(exposure, "contrast"),
        "gamma": _gamma_param(exposure),
        "shadows": _int_param(exposure, "shadows"),
        "highlights": _int_param(exposure, "highlights"),
        "channel_gains": sanitize_gains(exposure.get("channel_gains"),
                                        DEFAULT_GAINS),
    }


def _apply_exposure(img: Image.Image, exposure) -> Image.Image:
    """
    对图像应用曝光参数。**未生效时原样返回入参对象**（同一对象），
    调用方据此判断是否需要 close()。
    """
    params = _exposure_params(exposure)
    if params is None:
        return img
    return apply_exposure(img, **params)


def _read_cache_image(cache_file: str) -> Image.Image:
    """把缓存页读进内存（脱离文件句柄）。返回**新**图像，调用方负责 close()。"""
    with Image.open(cache_file) as src:
        return src.copy()


def _is_intact_jpeg(path: str) -> bool:
    """
    轻量校验缓存页是不是一张**完整**的 JPEG（只看头尾，不解码像素）。

    快路径（jpg 直接复制原始字节）必须先过这一关：否则损坏的缓存页
    （写盘中断 / 被截断 / 其实是 PDF 字节）会被原样复制成 .jpg 交付给用户，
    而调用方看到的 failed_pages=0 —— **静默交付坏文件**（N2）。

    判据：
      - 头 3 字节 = FFD8FF（SOI）
      - 尾部若干字节里能找到 FFD9（EOI）—— 允许文件末尾有少量填充字节
    代价恒定（两次小读），几百页也只是毫秒级。
    """
    try:
        with open(path, "rb") as f:
            if f.read(3) != b"\xff\xd8\xff":
                return False
            try:
                f.seek(-64, os.SEEK_END)
            except OSError:      # 文件比 64 字节还小
                return False
            return b"\xff\xd9" in f.read()
    except OSError:
        return False


def _expose_and_rotate(img: Image.Image, rotation: int, exposure,
                       deskew_angle: float = 0.0,
                       deskew_threshold=None) -> Image.Image:
    """
    按固定顺序 **曝光 → 手动旋转 → 自动纠偏** 处理图像。

    **接管 img 的所有权**：返回的图像由调用方负责 close（可能就是 img 本身）。
    顺序是硬约定：
      - 曝光作用于原始像素，必须最先做；
      - 手动旋转是 90° 的整数倍，自动纠偏是小角度 ⇒ **两者都要**
        （先转手动、再纠偏；两者都是平面旋转，交换顺序结果一致，
          但顺序固定下来才好验证）；
      - A4 画布归一由调用方在其后完成（600 DPI 不裁切那条护栏在 _a4_canvas 里）。

    ★ **纠偏角为 0（没测出来 / 低于施加阈值）时一个像素都不动** ——
      `deskew_image` 对 |angle| < 1e-9 原样返回，本函数也不会额外做
      convert/重采样 ⇒ "曝光 off + 直页" 的产物与改造前逐字节相同这条护栏不被破。

    deskew_angle: 扫描时测出来的纠偏角（度，PIL 角度语义，可直接施加）。
    deskew_threshold: 施加阈值（度）；None ⇒ 用配置里的 deskew_apply_threshold。
    """
    exposed = _apply_exposure(img, exposure)
    if exposed is not img:
        img.close()
        img = exposed
    if rotation:
        rotated = img.rotate(rotation, expand=True)
        img.close()
        img = rotated
    angle = deskew_angle_to_apply(deskew_angle, deskew_threshold)
    if angle:
        if img.mode == "P":
            # 调色板图的 fillcolor 是**调色板索引**，255 不一定是白 ⇒ 先转 RGB
            rgb = img.convert("RGB")
            img.close()
            img = rgb
        # 不 expand（画布尺寸不变）；四角填白由 skew_detect.deskew_image 按图像模式自动选
        # （★ RGB 不能传标量 255 —— Pillow 会当成打包整数 (255,0,0) 纯红）
        skewed = skew_detect.deskew_image(img, angle)
        if skewed is not img:
            img.close()
            img = skewed
    return img


def _a4_canvas(img, dpi: int = 300) -> Image.Image:
    """
    把图片居中放到 A4 画布上。

    ★ **永不裁切内容**（E-1）：
    画布固定按 300 DPI 计算（2480×3508），与扫描分辨率无关。
    旧实现直接 `paste`，当源图比画布大时偏移为负 ⇒ PIL **裁切**而不是缩放：
    600 DPI 扫描件（4960×7015）只剩中央 50%×50%，**页面 75% 的内容被切掉**。
    现在源图大于画布时先**等比缩放**到能放进去再居中贴；
    源图小于画布时保持"留白居中"。

    返回值**可能是入参本身**（入参已是 RGB 且尺寸恰好等于 A4 画布时），
    调用方必须用 `canvas is not img` 判断所有权，不能无条件 close() 入参。
    单页 PDF 与多页 PDF 共用这一个函数 —— 两者页面尺寸一致由此保证。

    ★ **返回值必须是「可继续使用」的图像**（N1）：
    旧实现把"即将返回的那张图"也放进了 finally 的 close() 里 ——
    灰度/黑白页（mode != "RGB"）恰好是 A4 尺寸时，返回的是一张**已关闭**的图，
    调用方 canvas.save() 抛 "Operation on closed image"，
    于是灰度 A4 页在多页 PDF 里被**整页静默丢弃**、单页时直接报错。
    现在：**尺寸精确匹配的分支提前 return，不经过 close 逻辑**；
    只有"新建画布"那条路径才 close 中间产物（像素已复制进画布，close 无害）。
    """
    a4_w = int(210 / 25.4 * dpi)
    a4_h = int(297 / 25.4 * dpi)
    w, h = img.size
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    if w == a4_w and h == a4_h:
        # 尺寸精确匹配 ⇒ 直接返回（可能是入参本身，也可能是转换后的副本）。
        # ★ 这里**不能** close()：返回值的所有权交给调用方。
        return rgb
    try:
        canvas = Image.new("RGB", (a4_w, a4_h), "white")
        if w > a4_w or h > a4_h:
            # 源图大于画布 ⇒ 等比缩小后居中（绝不裁切）
            scale = min(a4_w / w, a4_h / h)
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            resized = rgb.resize((new_w, new_h), Image.LANCZOS)
            try:
                canvas.paste(resized, ((a4_w - new_w) // 2, (a4_h - new_h) // 2))
            finally:
                resized.close()
        else:
            # 源图小于画布 ⇒ 留白居中
            canvas.paste(rgb, ((a4_w - w) // 2, (a4_h - h) // 2))
        return canvas
    finally:
        if rgb is not img:
            rgb.close()


def _save_a4_pdf(img, path, dpi=300):
    """把图片居中放到 A4 画布上，保存为标准尺寸 PDF（单页）"""
    canvas = _a4_canvas(img, dpi)
    try:
        canvas.save(path, "PDF", resolution=dpi)
    finally:
        if canvas is not img:
            canvas.close()


def _write_image(img, path, ext, dpi=300):
    """按扩展名落盘一张图片"""
    # 输出目录可能还不存在（PDF 分支会建，图片分支原先不会 ⇒ 目录缺失时整批失败）
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if ext == "pdf":
        _save_a4_pdf(img, path, dpi=dpi)
    elif ext in ("jpg", "jpeg"):
        img.convert("RGB").save(path, "JPEG", quality=95)
    elif ext == "png":
        img.save(path, "PNG")
    elif ext in ("tiff", "tif"):
        img.save(path, "TIFF")
    else:
        img.convert("RGB").save(path, "JPEG", quality=95)


def _extract_pdf_image(cache_file: str, rotation: int = 0) -> Image.Image:
    """
    从 PDF 缓存页里抽出内嵌图片。返回**新**图像，调用方负责 close()。

    正常扫描路径不会走到这里：打印机只出图片，缓存页是 JPEG/PNG/TIFF 字节。
    走到这里说明缓存页本身是 PDF（例如预览里手工裁边后被写成了 PDF），
    抽出来后仍然走同一条 A4 归一路径，页面尺寸依然一致。
    """
    from pypdf import PdfReader

    reader = PdfReader(cache_file)
    if not reader.pages:
        raise ValueError(f"PDF 缓存页没有页面: {cache_file}")
    images = reader.pages[0].images
    if not images:
        raise ValueError(f"PDF 缓存页里没有可用的图片: {cache_file}")
    with Image.open(io.BytesIO(images[0].data)) as src:
        return src.rotate(rotation, expand=True) if rotation else src.copy()


def _open_page_image(cache_file: str, rotation: int = 0, exposure=None,
                     deskew_angle: float = 0.0,
                     deskew_threshold=None) -> Image.Image:
    """
    打开一页缓存图片（必要时应用曝光、旋转与自动纠偏）。返回**新**图像，调用方负责 close()。

    处理顺序固定为 **曝光 → 旋转 → 纠偏**；A4 画布归一由调用方在其后完成。
    """
    try:
        img = _read_cache_image(cache_file)
    except UnidentifiedImageError:
        # 不是图片字节 → 当作 PDF 缓存页处理
        img = _extract_pdf_image(cache_file, 0)
    return _expose_and_rotate(img, rotation, exposure, deskew_angle,
                               deskew_threshold)


def _write_pdf(pages, out_path: str, dpi: int = 300, exposure=None,
               deskew_angles=None, deskew_threshold=None) -> tuple[int, int, list]:
    """
    合成 PDF（单页/多页同一份代码）。返回 (成功页数, 失败页数, 失败页下标列表)。

    两阶段：
      阶段一（逐页，内存里同时只有一页）：
        缓存页 → 曝光 → 旋转 → 纠偏 → A4 画布归一 → 写成单页 PDF 临时文件
      阶段二（逐页 append，不再载入图片像素）：
        PdfWriter.append(临时文件) → 写出最终 PDF
        ⚠ 阶段二会把整份 PDF 读进内存 ⇒ 峰值 ≈ 2.1 × 产物 PDF 体积，
          随页数线性增长（合并前用 estimate_pdf_peak_bytes 做体积预检）。

    临时文件放在系统临时目录（tempfile.mkdtemp），用完即删；
    不落缓存目录（会被 eviction 清掉，也会污染缓存大小统计）。

    ★ 最终 PDF **先写同目录临时文件、成功后再 os.replace**（E-2）：
    磁盘满 / MemoryError 中途失败时，最终路径上不会留下半截文件。
    """
    if PdfWriter is None:
        raise RuntimeError("缺少 pypdf 依赖，无法合成 PDF（请 pip install pypdf）")

    if deskew_threshold is None:
        deskew_threshold = load_deskew_settings()["deskew_apply_threshold"]

    tmp_dir = tempfile.mkdtemp(prefix="hp_scan_pdf_")
    tmp_files = []
    failed = 0
    failed_idx: list[int] = []
    try:
        for idx, (cache_file, rotation) in enumerate(pages, 1):
            tmp_pdf = os.path.join(tmp_dir, f"page_{idx:04d}.pdf")
            try:
                angle = 0.0
                if deskew_angles and idx - 1 < len(deskew_angles):
                    angle = deskew_angles[idx - 1]
                img = _open_page_image(cache_file, rotation, exposure,
                                       deskew_angle=angle,
                                       deskew_threshold=deskew_threshold)
                canvas = _a4_canvas(img, dpi)
                if canvas is not img:
                    img.close()
                try:
                    canvas.save(tmp_pdf, "PDF", resolution=dpi)
                finally:
                    canvas.close()
                tmp_files.append(tmp_pdf)
            except Exception as e:
                failed += 1
                failed_idx.append(idx - 1)
                logger.warning("PDF 页面转换失败，已跳过 [%s]: %s", cache_file, e)

        if not tmp_files:
            return 0, failed, failed_idx

        writer = PdfWriter()
        for tmp in tmp_files:
            writer.append(tmp)          # 逐页 append，不是一次性传列表
        _write_pdf_atomically(writer, out_path)
        return len(tmp_files), failed, failed_idx
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _write_pdf_atomically(writer, out_path: str) -> None:
    """
    把合并好的 PDF 写到最终路径：先写同目录临时文件，成功后 os.replace。

    失败（磁盘满 / MemoryError）时最终路径**不出现**半截文件，
    临时文件被清理掉。
    """
    directory = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=os.path.basename(out_path) + ".", suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as f:
            writer.write(f)
        os.replace(tmp_path, out_path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def copy_to_extra_dirs(saved_files, extra_dirs) -> None:
    """复制到额外输出目录。单个目录失败不影响主产物。"""
    for extra_dir in extra_dirs or []:
        try:
            os.makedirs(extra_dir, exist_ok=True)
            for fpath in saved_files:
                extra_path = os.path.join(extra_dir, os.path.basename(fpath))
                if os.path.abspath(extra_path) != os.path.abspath(fpath):
                    shutil.copy2(fpath, extra_path)
        except Exception as e:
            logger.debug("复制到额外目录失败 [%s]: %s", extra_dir, e)


def _remove_page_meta(job_id: str) -> None:
    """清理该 job 的页元数据（失败不影响保存结果）"""
    try:
        page_meta.PageMetaStore(cache_manager._cache_root()).remove_job(job_id)
    except Exception as e:
        logger.debug("清理页元数据失败 [%s]: %s", job_id, e)


def _finalize(saved_files, job_id, extra_dirs, device_name, device_ip,
              page_count, ext, exposure=None):
    """额外目录复制 + 清缓存 + 清页元数据 + 写历史

    page_count 必须是**实际写出的页数**（不是请求的页数）：
    有页被跳过时，历史里的页数必须与产物一致，否则"历史显示 30 页、
    实际 PDF 只有 25 页"这种不一致会一直误导用户（F-4）。
    """
    copy_to_extra_dirs(saved_files, extra_dirs)
    if job_id:
        cache_manager.remove_job(job_id)
        _remove_page_meta(job_id)
    if saved_files:
        history_manager.append({
            "device_name": device_name,
            "device_ip": device_ip,
            "page_count": page_count,
            "format": ext,
            "file_path": saved_files[0],
            "file_count": len(saved_files),
            "exposure": exposure,
        })
    return saved_files


def save_single(cache_path: str, ext: str, output_path: str, rotation: int = 0,
                extra_dirs=None, job_id: str = None,
                device_name: str = "", device_ip: str = "",
                exposure=None) -> str:
    """
    单页保存。返回实际落盘路径。

    exposure: dict | None，形如
        {"mode": "auto" | "manual",
         "brightness": int, "contrast": int, "gamma": float,
         "shadows": int, "highlights": int,
         "channel_gains": (r, g, b)}
    曝光在 **旋转之前、纠偏之前、A4 画布归一之前** 应用
    （顺序固定：曝光 → 旋转 → 纠偏 → A4 归一）。
    exposure=None 或 mode="off" ⇒ 行为与改造前完全一致（向后兼容）。

    自动纠偏：纠偏角取自该 job 的页元数据（page_meta.deskew_angle，
    扫描时测出来的）；`deskew_enabled=False` 或角度低于施加阈值 ⇒ 不纠偏，
    产物与改造前逐字节相同。

    一律用 with 打开图片：显式管理文件句柄，不依赖 Pillow 内部
    「读取后自动关句柄」的实现细节（该细节随版本变化）。
    """
    final = output_path
    if not final.lower().endswith(f".{ext}"):
        final = f"{final.rsplit('.', 1)[0]}.{ext}"

    settings = load_deskew_settings()
    deskew_angle = 0.0
    if settings["deskew_enabled"] and job_id:
        angles = resolve_deskew_angles(job_id, [(cache_path, rotation)])
        deskew_angle = angles[0] if angles else 0.0
    deskew_threshold = settings["deskew_apply_threshold"]

    if ext == "pdf":
        # PDF 走本地合成，和多页 PDF 同一份代码 → 页面尺寸必然一致
        ok, _failed, _failed_idx = _write_pdf(
            [(cache_path, rotation)], final, exposure=exposure,
            deskew_angles=[deskew_angle], deskew_threshold=deskew_threshold)
        if not ok:
            raise RuntimeError(f"PDF 生成失败（缓存页无法转换为图片）: {cache_path}")
    else:
        img = _read_cache_image(cache_path)
        img = _expose_and_rotate(img, rotation, exposure,
                                  deskew_angle=deskew_angle,
                                  deskew_threshold=deskew_threshold)
        try:
            _write_image(img, final, ext)
        finally:
            img.close()

    _finalize([final], job_id, extra_dirs, device_name, device_ip, 1, ext,
              exposure=exposure)
    return final


def _precheck_pdf_volume(page_specs, confirm, warnings) -> None:
    """
    PDF 合并前的**体积预检**（F-5）。

    阶段二峰值 ≈ 2.1 × 产物 PDF 体积，随页数线性增长。估算峰值超过
    门禁（1.5GB）时，在**开始合并前**提示用户，并给"仍然继续"的选项 ——
    绝不静默截断（少页比失败更糟）。

    confirm 为 None（无 GUI 回调）时：只记 warning 并继续，
    提示文案进 warnings，交给调用方展示。
    """
    peak = estimate_pdf_peak_bytes(page_specs)
    if peak < PDF_PEAK_LIMIT_BYTES:
        return
    msg = (f"预计 PDF 合成内存峰值约 {peak / 1024 / 1024:.0f}MB，"
           f"超过 {PDF_PEAK_LIMIT_BYTES / 1024 / 1024:.0f}MB 门禁，"
           f"可能耗尽内存导致保存失败。仍要继续吗？")
    if confirm is None:
        logger.warning("%s", msg)
        warnings.append(msg)
        return
    if not confirm(msg):
        raise SaveAborted(msg)


def save_pages(job_id: str, ext: str, output_path: str,
               page_specs: list[tuple[str, int]], groups: list[list[int]],
               extra_dirs=None,
               device_name: str = "", device_ip: str = "",
               exposure=None,
               confirm: Callable | None = None) -> SaveResult:
    """
    多页保存（可分组）。

    page_specs: [(缓存文件路径, 旋转角度), ...]，按页序
    groups:     对 page_specs 的索引分组，list[list[int]]
    exposure:   曝光参数（文档级，所有页同一份），语义同 save_single。
    confirm:    体积预检的确认回调（收到提示文案，返回 False 表示放弃保存）。
                None ⇒ 只记 warning 并继续（提示进返回值的 .warnings）。

    自动纠偏（批次 4）：逐页纠偏角取自页元数据（page_meta.deskew_angle），
    顺序为 **曝光 → 手动旋转 → 自动纠偏 → A4 归一**；
    deskew_enabled=False 或角度低于 deskew_apply_threshold ⇒ 该页不纠偏。

    返回 **SaveResult**（list 子类）：
      - 迭代 / len / [0] 与普通 list 完全一致（现有调用方不受影响）
      - `.failed_pages`   失败页数
      - `.skipped_pages`  被跳过的页在 page_specs 里的下标（0 基）
      - `.saved_page_count` 实际写出的页数
      - `.warnings`       提示文案（如体积预检提示）
    """
    base = output_path.rsplit(".", 1)[0]
    group_count = len(groups)
    saved_files = []
    failed_pages = 0
    skipped_pages: list[int] = []
    warnings: list[str] = []
    saved_page_count = 0
    exposure_active = _exposure_params(exposure) is not None

    # ── 自动纠偏（批次 4）────────────────────────────────────────────
    # 纠偏角来自页元数据（扫描时测出来的），逐页解析：
    #   ① 缓存路径精确匹配 ② 文件名里的页号 ③ page_specs 下标 + 1
    # deskew_enabled=False ⇒ 一页都不纠偏（只检测不纠偏的总开关）。
    settings = load_deskew_settings()
    deskew_threshold = settings["deskew_apply_threshold"]
    if settings["deskew_enabled"]:
        deskew_angles = resolve_deskew_angles(job_id, page_specs)
    else:
        deskew_angles = [0.0] * len(page_specs or [])

    for gi, group_indices in enumerate(groups, 1):
        if ext == "pdf":
            pages = [page_specs[page_idx] for page_idx in group_indices]
            group_angles = [deskew_angles[page_idx] if page_idx < len(deskew_angles)
                            else 0.0 for page_idx in group_indices]
            pdf_path = build_output_name(base, ext, group_count, gi, 1)
            # 体积预检：在开始合并**之前**提示（不静默截断）
            _precheck_pdf_volume(pages, confirm, warnings)
            ok, failed, failed_idx = _write_pdf(pages, pdf_path, exposure=exposure,
                                                deskew_angles=group_angles,
                                                deskew_threshold=deskew_threshold)
            failed_pages += failed
            # failed_idx 是组内下标 ⇒ 映射回 page_specs 的全局下标
            skipped_pages.extend(group_indices[i] for i in failed_idx)
            saved_page_count += ok
            if not ok:
                logger.warning("PDF 分组 %d 没有任何页面转换成功，跳过: %s", gi, pdf_path)
                continue
            saved_files.append(pdf_path)
        else:
            for seq, page_idx in enumerate(group_indices, 1):
                cache_file, rotation = page_specs[page_idx]
                angle = deskew_angles[page_idx] if page_idx < len(deskew_angles) else 0.0
                apply_angle = deskew_angle_to_apply(angle, deskew_threshold)
                try:
                    fpath = build_output_name(base, ext, group_count, gi, seq)
                    if (ext in ("jpg", "jpeg") and not rotation
                            and not exposure_active and not apply_angle
                            and _is_intact_jpeg(cache_file)):
                        # 无曝光、无旋转、无纠偏 ⇒ 直接复制原始字节（快路径，逐字节一致）
                        shutil.copy2(cache_file, fpath)
                    else:
                        # 快路径的前置条件不满足（曝光/旋转/纠偏，或缓存页不是完整 JPEG）
                        # ⇒ 走解码路径：能解码就正常落盘，解不开就由下面的
                        #    except 计入 failed_pages —— **绝不谎报成功**（N2）。
                        img = _open_page_image(cache_file, rotation, exposure,
                                               deskew_angle=angle,
                                               deskew_threshold=deskew_threshold)
                        try:
                            _write_image(img, fpath, ext)
                        finally:
                            img.close()
                    saved_files.append(fpath)
                    saved_page_count += 1
                except Exception as e:
                    failed_pages += 1
                    skipped_pages.append(page_idx)
                    logger.warning("保存页失败 [%s]: %s", cache_file, e)

    # 一页都没存下来时必须报错：否则调用方（GUI）会"提示成功但产出 0 个文件"
    # ★ 空页列表与"全部失败"同等对待（N5）：旧写法 `and page_specs` 让
    #   page_specs=[] 短路 ⇒ 返回 ok=False 的空 SaveResult，调用方只看
    #   len(result) 就会误报成功。现在返回即非空：要么有文件，要么抛异常。
    if not saved_files:
        if not page_specs:
            raise RuntimeError("没有可保存的页面（页列表为空）")
        raise RuntimeError(
            f"没有成功保存任何页面（共 {len(page_specs)} 页，失败 {failed_pages} 页）")

    # 历史里的页数必须是**实际写出的页数**，不能是请求的页数
    _finalize(saved_files, job_id, extra_dirs, device_name, device_ip,
              saved_page_count or len(saved_files), ext, exposure=exposure)
    return SaveResult(saved_files, failed_pages=failed_pages,
                       skipped_pages=skipped_pages, warnings=warnings,
                       saved_page_count=saved_page_count)


# ── 会话级落盘（批次 4 · B4-5）─────────────────────────────────────────
#
# 一个**文档会话**（DocumentSession）把多次扫描聚合成一个文档，落盘时：
#   · 双面 ⇒ 正面/背面交错合并成**一个** PDF
#   · 批次 ⇒ N 个批次的页顺序拼接成**一个** PDF
#   · 图片 ⇒ 每页一个文件，命名带**全局页号**（跨批次连续）
#
# ★ **合并规则一律委托 doc_merge**：边界规则（末页只有一面、背面整叠翻面）
#   在 doc_merge 里已有测试覆盖 ⇒ 这里**不重写**，只负责"取页 + 喂给它"。
# ★ **纠偏施加复用 c6037a4 已落地的路径**：本节只负责"把角度取出来"，
#   旋转本身仍由 _expose_and_rotate → skew_detect.deskew_image 完成。
# ★ 会话与缓存目录的关系：会话引用的 job 目录是**受保护**的
#   （document_session → cache_manager.protect_dir）⇒ 落盘成功后必须
#   **先解除保护、再删目录**，否则目录永远清不掉。


def _strip_output_ext(path: str) -> str:
    """
    去掉输出路径的扩展名。

    ★ 只动**文件名**部分：目录名里带点（"/tmp/a.b/out"）不会被误切
      —— os.path.splitext 只看 basename。
    """
    text = str(path)
    root, name = os.path.split(text)
    stem, ext = os.path.splitext(name)
    if not ext:
        return text
    return os.path.join(root, stem) if root else stem


def _session_merged_pages(session) -> list:
    """
    会话页的**最终页序**（合并规则全部委托 doc_merge）。

    · mode == "duplex" ⇒ 按 side 拆正/反 ⇒
      `doc_merge.interleave_duplex(front, back, reverse_back=session.reverse_back)`
      ★ side == "single" 的页按**正面**处理（双面会话里它就是被扫到的那一面）
    · mode in ("batch", "single") ⇒ 按 job_id 分组（保持**首次出现顺序**，
      组内保持原序）⇒ `doc_merge.merge_batches(batches)`
    """
    pages = list(getattr(session, "pages", None) or [])
    mode = str(getattr(session, "mode", "single") or "single")
    if mode == "duplex":
        front = [p for p in pages if str(getattr(p, "side", "single")) != "back"]
        back = [p for p in pages if str(getattr(p, "side", "single")) == "back"]
        return list(doc_merge.interleave_duplex(
            front, back,
            reverse_back=bool(getattr(session, "reverse_back", True))))
    batches: list = []
    index: dict = {}
    for page in pages:
        job_id = str(getattr(page, "job_id", "") or "")
        if job_id not in index:
            index[job_id] = len(batches)
            batches.append([])
        batches[index[job_id]].append(page)
    return list(doc_merge.merge_batches(batches))


def _session_job_meta(job_id: str) -> dict:
    """
    读某 job 的页元数据。

    ★ **键是字符串页号**（str(page_no)），不是 int —— 见 page_meta.set_page。
    """
    if not job_id:
        return {}
    try:
        store = page_meta.PageMetaStore(cache_manager._cache_root())
        job = store.get_job(job_id) or {}
    except Exception as e:
        logger.debug("读会话页元数据失败 [%s]: %s", job_id, e)
        return {}
    return job if isinstance(job, dict) else {}


def _session_page_specs(merged_pages, deskew_enabled: bool = True):
    """
    逐页解析落盘参数 ⇒ (page_specs, deskew_angles)。

    page_specs 与既有 save_pages **同构**：[(缓存文件路径, 手动旋转角), ...]

    ★ 纠偏角来源（按优先级）：
      ① page_meta[(job_id, str(page_no))]["deskew_angle"]  ← 权威
        （扫描时由 skew_detect 测出、写进页元数据）
      ② `resolve_deskew_angles` —— c6037a4 已落盘的解析路径
        （缓存路径精确匹配 → 文件名里的页号 → 下标+1）
      ③ PageRef 自带的 deskew_angle（会话自己的副本）
    ★ 手动旋转角同理：page_meta 优先，PageRef.manual_rotation 兜底。
    ★ `PageRef.force_keep == True` ⇒ 角度强制归零
      （用户选了「强制保存」⇒ 这一页**不纠偏**）。
    """
    by_job: dict = {}
    for idx, page in enumerate(merged_pages):
        by_job.setdefault(str(getattr(page, "job_id", "") or ""), []).append(idx)

    specs: list = [None] * len(merged_pages)
    angles: list = [0.0] * len(merged_pages)

    for job_id, indices in by_job.items():
        job_meta = _session_job_meta(job_id)
        # ② c6037a4 的既有解析路径（按 job 分组调用，避免逐页重读元数据）
        resolved: list = []
        if job_id:
            try:
                resolved = list(resolve_deskew_angles(
                    job_id, [(merged_pages[i].cache_path, 0) for i in indices]))
            except Exception as e:
                logger.debug("解析会话页纠偏角失败 [%s]: %s", job_id, e)
                resolved = []
        for pos, idx in enumerate(indices):
            page = merged_pages[idx]
            cache_path = str(getattr(page, "cache_path", "") or "")
            page_no = int(getattr(page, "page_no", 0) or 0)
            meta = job_meta.get(str(page_no))
            meta = meta if isinstance(meta, dict) else {}

            rotation = _as_int(meta.get("manual_rotation", 0), 0, 0)
            if not rotation:
                rotation = _as_int(getattr(page, "manual_rotation", 0), 0, 0)

            angle = 0.0
            if "deskew_angle" in meta:
                angle = _as_float(meta.get("deskew_angle"), 0.0, minimum=-180.0)
            else:
                if pos < len(resolved):
                    angle = float(resolved[pos] or 0.0)
                if not angle:
                    angle = _as_float(getattr(page, "deskew_angle", 0.0), 0.0,
                                      minimum=-180.0)
            if getattr(page, "force_keep", False):
                angle = 0.0
            if not deskew_enabled:
                angle = 0.0

            specs[idx] = (cache_path, rotation)
            angles[idx] = angle

    return specs, angles


def _session_cleanup_jobs(job_ids) -> None:
    """
    落盘成功后的缓存清理：**先解除保护、再删目录**（否则目录永远清不掉）。

    ★ `cache_manager.unprotect_dir` 由 B4-2 落地，与本模块并行 ⇒ 用 getattr
      做**存在性检查**：没有就记 debug 日志跳过（**不因此让保存失败**）。
      与 document_session._unprotect_jobs 同一套路。
    """
    unprotect = getattr(cache_manager, "unprotect_dir", None)
    if unprotect is None:
        logger.debug("cache_manager.unprotect_dir 尚未落地，跳过解除保护: %s",
                     list(job_ids))
    else:
        for jid in job_ids:
            try:
                unprotect(jid)
            except Exception as e:
                logger.error("缓存目录解除保护失败: %s（%s）", jid, e)
    for jid in job_ids:
        try:
            cache_manager.remove_job(jid)
        except Exception as e:
            logger.error("缓存目录删除失败: %s（%s）", jid, e)
        _remove_page_meta(jid)


def save_session(session, out_path=None, extra_dirs=None,
                 job_id=None, device_name="", device_ip="",
                 groups=None, confirm=None) -> SaveResult:
    """
    把一个**文档会话**的全部页合并落盘（批次 4 · B4-5）。

    页序（合并规则一律委托 doc_merge，本函数**不重写**）：
      · mode == "duplex" ⇒ 按 side 拆正/反 ⇒
        `doc_merge.interleave_duplex(front, back, reverse_back=session.reverse_back)`
      · mode in ("batch", "single") ⇒ 按 job_id 分组（保持**首次出现顺序**，
        组内保持原序）⇒ `doc_merge.merge_batches(batches)`

    产物：
      · ext == "pdf" ⇒ 合成**一个** PDF（复用既有两阶段流式合成 _write_pdf）
      · jpg/png/tiff ⇒ 每页一个文件，命名带**全局页号**（跨批次连续）

    纠偏：角度取自页元数据 page_meta[(job_id, str(page_no))]，施加走 c6037a4
    已落地的路径（_expose_and_rotate → skew_detect.deskew_image）；
    `PageRef.force_keep == True` 的页**不纠偏**。

    清理（**只在全部页都合成成功**时执行；失败时什么都不清 ⇒ 可重试）：
      ① 先解除保护（cache_manager.unprotect_dir，可能尚未落地 ⇒ 存在性检查）
      ② 再删 job 目录（cache_manager.remove_job）+ 页元数据

    :param session: 文档会话（鸭子类型：需要 pages / mode / ext / dpi /
        exposure / reverse_back / output_path）
    :param out_path: 输出路径；缺省用 session.output_path
    :param extra_dirs: 额外输出目录（复制一份过去）
    :param job_id: 额外要清理的 job_id（缺省只清会话引用过的 job）
    :param groups: 可选，list[list[int]] —— 对**合并后页序**的下标分组
        （多组产物，命名走 build_output_name 的多组规则）；缺省 = 一组
    :param confirm: 体积预检的确认回调（返回 False ⇒ 抛 SaveAborted，
        **不静默截断**）；None ⇒ 只记 warning 并继续
    :return: **SaveResult**（list 子类）
    """
    if session is None:
        raise ValueError("session 不能为空")
    if not list(getattr(session, "pages", None) or []):
        # 空会话 ⇒ 明确报错，**不产出 0 字节文件**
        raise RuntimeError("会话没有可保存的页面（页列表为空）")

    ext = str(getattr(session, "ext", "pdf") or "pdf").lower()
    target = str(out_path or getattr(session, "output_path", "") or "")
    if not target:
        raise ValueError("没有指定输出路径（out_path 为空且会话未设置 output_path）")
    base = _strip_output_ext(target)

    merged = _session_merged_pages(session)
    if not merged:
        raise RuntimeError("会话没有可保存的页面（合并后为空）")

    dpi = int(getattr(session, "dpi", 300) or 300)
    exposure = getattr(session, "exposure", None)
    exposure_active = _exposure_params(exposure) is not None

    # ── 逐页落盘参数（纠偏角 / 手动旋转）────────────────────────────
    settings = load_deskew_settings()
    deskew_threshold = settings["deskew_apply_threshold"]
    page_specs, deskew_angles = _session_page_specs(
        merged, deskew_enabled=settings["deskew_enabled"])

    # ── 分组（缺省：一组 = 全部页）───────────────────────────────────
    if groups is None:
        group_indices = [list(range(len(merged)))]
    else:
        group_indices = []
        for group in groups:
            idxs = [int(i) for i in group]
            for i in idxs:
                if i < 0 or i >= len(merged):
                    raise ValueError(f"分组下标越界: {i}（共 {len(merged)} 页）")
            group_indices.append(idxs)
        if not group_indices:
            raise ValueError("groups 为空 ⇒ 没有可保存的页面")
    group_count = len(group_indices)

    saved_files: list = []
    failed_pages = 0
    skipped_pages: list = []
    warnings: list = []
    saved_page_count = 0

    for gi, indices in enumerate(group_indices, 1):
        if ext == "pdf":
            group_pages = [page_specs[i] for i in indices]
            group_angles = [deskew_angles[i] for i in indices]
            pdf_path = build_output_name(base, ext, group_count, gi, 1)
            # 体积预检：在开始合并**之前**提示（不静默截断）
            _precheck_pdf_volume(group_pages, confirm, warnings)
            ok, failed, failed_idx = _write_pdf(
                group_pages, pdf_path, dpi=dpi, exposure=exposure,
                deskew_angles=group_angles, deskew_threshold=deskew_threshold)
            failed_pages += failed
            # failed_idx 是组内下标 ⇒ 映射回合并后页序的全局下标
            skipped_pages.extend(indices[i] for i in failed_idx)
            saved_page_count += ok
            if not ok:
                logger.warning("会话 PDF 分组 %d 没有任何页面转换成功，跳过: %s",
                               gi, pdf_path)
                continue
            saved_files.append(pdf_path)
        else:
            for seq, idx in enumerate(indices, 1):
                cache_file, rotation = page_specs[idx]
                angle = deskew_angles[idx]
                apply_angle = deskew_angle_to_apply(angle, deskew_threshold)
                try:
                    fpath = build_output_name(base, ext, group_count, gi, seq)
                    if (ext in ("jpg", "jpeg") and not rotation
                            and not exposure_active and not apply_angle
                            and _is_intact_jpeg(cache_file)):
                        # 无曝光、无旋转、无纠偏 ⇒ 直接复制原始字节（快路径）
                        shutil.copy2(cache_file, fpath)
                    else:
                        img = _open_page_image(cache_file, rotation, exposure,
                                               deskew_angle=angle,
                                               deskew_threshold=deskew_threshold)
                        try:
                            _write_image(img, fpath, ext)
                        finally:
                            img.close()
                    saved_files.append(fpath)
                    saved_page_count += 1
                except Exception as e:
                    failed_pages += 1
                    skipped_pages.append(idx)
                    logger.warning("会话页保存失败 [%s]: %s", cache_file, e)

    # 一页都没存下来 ⇒ 明确报错（**不产出 0 字节文件**），且**什么都不清**
    if not saved_files:
        raise RuntimeError(
            f"没有成功保存任何页面（共 {len(merged)} 页，失败 {failed_pages} 页）")

    copy_to_extra_dirs(saved_files, extra_dirs)

    # 清理：只在**全部页都合成成功**时清（部分失败 ⇒ 保留缓存以便重试）
    job_ids = [str(j) for j in getattr(session, "referenced_job_ids", []) or []]
    if job_id and str(job_id) not in job_ids:
        job_ids.append(str(job_id))
    if failed_pages:
        warnings.append(
            f"有 {failed_pages} 页未能保存，缓存目录已保留（可重试）："
            f"{', '.join(job_ids)}")
        logger.warning("会话落盘有 %d 页失败 ⇒ 保留缓存目录以便重试", failed_pages)
    else:
        _session_cleanup_jobs(job_ids)

    if saved_files:
        history_manager.append({
            "device_name": device_name,
            "device_ip": device_ip,
            "page_count": saved_page_count,
            "format": ext,
            "file_path": saved_files[0],
            "file_count": len(saved_files),
            "exposure": exposure,
        })

    return SaveResult(saved_files, failed_pages=failed_pages,
                       skipped_pages=skipped_pages, warnings=warnings,
                       saved_page_count=saved_page_count)
