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
"""

import io
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field

from PIL import Image, UnidentifiedImageError

import cache_manager
import history_manager
import page_meta
from exposure import (DEFAULT_GAMMA, DEFAULT_GAINS, GAMMA_MAX, GAMMA_MIN,
                      PARAM_MAX, PARAM_MIN, apply_exposure, sanitize_gains,
                      sanitize_int, sanitize_number)

try:
    from pypdf import PdfWriter
except ImportError:  # pragma: no cover - 缺依赖时给明确报错，而不是导入就炸
    PdfWriter = None

logger = logging.getLogger(__name__)

__all__ = [
    "build_output_name", "copy_to_extra_dirs", "save_pages", "save_single",
    "estimate_pdf_peak_bytes", "run_save_job",
    "SaveRequest", "SaveResult", "SaveAborted",
    "PDF_PEAK_FACTOR", "PDF_PEAK_LIMIT_BYTES",
]

# PDF 合成的内存峰值系数：实测阶段二峰值 ≈ 2.1 × 最终 PDF 体积
PDF_PEAK_FACTOR = 2.1
# PDF 合成的内存峰值门禁（与批次 4 门禁一致）：1.5GB
PDF_PEAK_LIMIT_BYTES = int(1.5 * 1024 ** 3)


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


def _expose_and_rotate(img: Image.Image, rotation: int, exposure) -> Image.Image:
    """
    按固定顺序 **曝光 → 旋转** 处理图像。

    **接管 img 的所有权**：返回的图像由调用方负责 close（可能就是 img 本身）。
    顺序是硬约定：曝光作用于原始像素，旋转只是几何变换，不能颠倒。
    """
    exposed = _apply_exposure(img, exposure)
    if exposed is not img:
        img.close()
        img = exposed
    if rotation:
        rotated = img.rotate(rotation, expand=True)
        img.close()
        img = rotated
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


def _open_page_image(cache_file: str, rotation: int = 0, exposure=None) -> Image.Image:
    """
    打开一页缓存图片（必要时应用曝光与旋转）。返回**新**图像，调用方负责 close()。

    处理顺序固定为 **曝光 → 旋转**；A4 画布归一由调用方在其后完成。
    """
    try:
        img = _read_cache_image(cache_file)
    except UnidentifiedImageError:
        # 不是图片字节 → 当作 PDF 缓存页处理
        img = _extract_pdf_image(cache_file, 0)
    return _expose_and_rotate(img, rotation, exposure)


def _write_pdf(pages, out_path: str, dpi: int = 300, exposure=None) -> tuple[int, int, list]:
    """
    合成 PDF（单页/多页同一份代码）。返回 (成功页数, 失败页数, 失败页下标列表)。

    两阶段：
      阶段一（逐页，内存里同时只有一页）：
        缓存页 → 曝光 → 旋转 → A4 画布归一 → 写成单页 PDF 临时文件
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

    tmp_dir = tempfile.mkdtemp(prefix="hp_scan_pdf_")
    tmp_files = []
    failed = 0
    failed_idx: list[int] = []
    try:
        for idx, (cache_file, rotation) in enumerate(pages, 1):
            tmp_pdf = os.path.join(tmp_dir, f"page_{idx:04d}.pdf")
            try:
                img = _open_page_image(cache_file, rotation, exposure)
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
    曝光在 **旋转之前、A4 画布归一之前** 应用（顺序固定：曝光 → 旋转 → A4 归一）。
    exposure=None 或 mode="off" ⇒ 行为与改造前完全一致（向后兼容）。

    一律用 with 打开图片：显式管理文件句柄，不依赖 Pillow 内部
    「读取后自动关句柄」的实现细节（该细节随版本变化）。
    """
    final = output_path
    if not final.lower().endswith(f".{ext}"):
        final = f"{final.rsplit('.', 1)[0]}.{ext}"

    if ext == "pdf":
        # PDF 走本地合成，和多页 PDF 同一份代码 → 页面尺寸必然一致
        ok, _failed, _failed_idx = _write_pdf([(cache_path, rotation)], final,
                                              exposure=exposure)
        if not ok:
            raise RuntimeError(f"PDF 生成失败（缓存页无法转换为图片）: {cache_path}")
    else:
        img = _read_cache_image(cache_path)
        img = _expose_and_rotate(img, rotation, exposure)
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

    for gi, group_indices in enumerate(groups, 1):
        if ext == "pdf":
            pages = [page_specs[page_idx] for page_idx in group_indices]
            pdf_path = build_output_name(base, ext, group_count, gi, 1)
            # 体积预检：在开始合并**之前**提示（不静默截断）
            _precheck_pdf_volume(pages, confirm, warnings)
            ok, failed, failed_idx = _write_pdf(pages, pdf_path, exposure=exposure)
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
                try:
                    fpath = build_output_name(base, ext, group_count, gi, seq)
                    if (ext in ("jpg", "jpeg") and not rotation
                            and not exposure_active
                            and _is_intact_jpeg(cache_file)):
                        # 无曝光且无旋转 ⇒ 直接复制原始字节（快路径，逐字节一致）
                        shutil.copy2(cache_file, fpath)
                    else:
                        # 快路径的前置条件不满足（曝光/旋转，或缓存页不是完整 JPEG）
                        # ⇒ 走解码路径：能解码就正常落盘，解不开就由下面的
                        #    except 计入 failed_pages —— **绝不谎报成功**（N2）。
                        img = _open_page_image(cache_file, rotation, exposure)
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
