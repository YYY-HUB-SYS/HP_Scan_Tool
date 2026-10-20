"""
保存落盘管理器 — HP Scan Tool v4.0
把扫描结果写入目标目录：格式转换 / 分组命名 / 额外目录复制 / 缓存清理 / 历史写入。
纯 Python 实现，不依赖 Qt 与任何 GUI，可独立单元测试。

PDF 路径（批次 2）：
- 打印机只出图片，PDF 在本地合成（见 escl_engine.REQUEST_FORMAT）
- 多页 PDF 两阶段流式合成，内存不随页数线性增长
- 单页与多页 PDF 共用 _a4_canvas，页面尺寸必然一致
"""

import io
import logging
import os
import shutil
import tempfile

from PIL import Image, UnidentifiedImageError

import cache_manager
import history_manager
import page_meta

try:
    from pypdf import PdfWriter
except ImportError:  # pragma: no cover - 缺依赖时给明确报错，而不是导入就炸
    PdfWriter = None

logger = logging.getLogger(__name__)

__all__ = ["build_output_name", "copy_to_extra_dirs", "save_pages", "save_single"]


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


def _a4_canvas(img, dpi: int = 300) -> Image.Image:
    """
    把图片居中放到 A4 画布上（不拉伸；超出画布的部分被裁掉）。

    返回值**可能是入参本身**（入参已是 RGB 且尺寸恰好等于 A4 画布时），
    调用方必须用 `canvas is not img` 判断所有权，不能无条件 close() 入参。
    单页 PDF 与多页 PDF 共用这一个函数 —— 两者页面尺寸一致由此保证。
    """
    a4_w = int(210 / 25.4 * dpi)
    a4_h = int(297 / 25.4 * dpi)
    w, h = img.size
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    try:
        if w == a4_w and h == a4_h:
            return rgb
        canvas = Image.new("RGB", (a4_w, a4_h), "white")
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


def _open_page_image(cache_file: str, rotation: int = 0) -> Image.Image:
    """
    打开一页缓存图片（必要时应用旋转）。返回**新**图像，调用方负责 close()。
    """
    try:
        with Image.open(cache_file) as src:
            return src.rotate(rotation, expand=True) if rotation else src.copy()
    except UnidentifiedImageError:
        # 不是图片字节 → 当作 PDF 缓存页处理
        return _extract_pdf_image(cache_file, rotation)


def _write_pdf(pages, out_path: str, dpi: int = 300) -> tuple[int, int]:
    """
    流式合成 PDF（单页/多页同一份代码）。返回 (成功页数, 失败页数)。

    两阶段：
      阶段一（逐页，内存里同时只有一页）：
        缓存页 → A4 画布归一 → 写成单页 PDF 临时文件
      阶段二（逐页 append，不再载入图片像素）：
        PdfWriter.append(临时文件) → 写出最终 PDF

    临时文件放在系统临时目录（tempfile.mkdtemp），用完即删；
    不落缓存目录（会被 eviction 清掉，也会污染缓存大小统计）。
    """
    if PdfWriter is None:
        raise RuntimeError("缺少 pypdf 依赖，无法合成 PDF（请 pip install pypdf）")

    tmp_dir = tempfile.mkdtemp(prefix="hp_scan_pdf_")
    tmp_files = []
    failed = 0
    try:
        for idx, (cache_file, rotation) in enumerate(pages, 1):
            tmp_pdf = os.path.join(tmp_dir, f"page_{idx:04d}.pdf")
            try:
                img = _open_page_image(cache_file, rotation)
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
                logger.warning("PDF 页面转换失败，已跳过 [%s]: %s", cache_file, e)

        if not tmp_files:
            return 0, failed

        writer = PdfWriter()
        for tmp in tmp_files:
            writer.append(tmp)          # 逐页 append，不是一次性传列表
        with open(out_path, "wb") as f:
            writer.write(f)
        return len(tmp_files), failed
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
              page_count, ext):
    """额外目录复制 + 清缓存 + 清页元数据 + 写历史"""
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
        })
    return saved_files


def save_single(cache_path: str, ext: str, output_path: str, rotation: int = 0,
                extra_dirs=None, job_id: str = None,
                device_name: str = "", device_ip: str = "") -> str:
    """
    单页保存。返回实际落盘路径。

    一律用 with 打开图片：显式管理文件句柄，不依赖 Pillow 内部
    「读取后自动关句柄」的实现细节（该细节随版本变化）。
    """
    final = output_path
    if not final.lower().endswith(f".{ext}"):
        final = f"{final.rsplit('.', 1)[0]}.{ext}"

    if ext == "pdf":
        # PDF 走本地合成，和多页 PDF 同一份代码 → 页面尺寸必然一致
        ok, _failed = _write_pdf([(cache_path, rotation)], final)
        if not ok:
            raise RuntimeError(f"PDF 生成失败（缓存页无法转换为图片）: {cache_path}")
    else:
        with Image.open(cache_path) as src:
            img = src.rotate(rotation, expand=True) if rotation else src.copy()
            _write_image(img, final, ext)
            img.close()

    _finalize([final], job_id, extra_dirs, device_name, device_ip, 1, ext)
    return final


def save_pages(job_id: str, ext: str, output_path: str,
               page_specs: list[tuple[str, int]], groups: list[list[int]],
               extra_dirs=None,
               device_name: str = "", device_ip: str = "") -> list[str]:
    """
    多页保存（可分组）。

    page_specs: [(缓存文件路径, 旋转角度), ...]，按页序
    groups:     对 page_specs 的索引分组，list[list[int]]
    返回实际落盘的文件路径列表
    """
    base = output_path.rsplit(".", 1)[0]
    group_count = len(groups)
    saved_files = []
    failed_pages = 0

    for gi, group_indices in enumerate(groups, 1):
        if ext == "pdf":
            pages = [page_specs[page_idx] for page_idx in group_indices]
            pdf_path = build_output_name(base, ext, group_count, gi, 1)
            ok, failed = _write_pdf(pages, pdf_path)
            failed_pages += failed
            if not ok:
                logger.warning("PDF 分组 %d 没有任何页面转换成功，跳过: %s", gi, pdf_path)
                continue
            saved_files.append(pdf_path)
        else:
            for seq, page_idx in enumerate(group_indices, 1):
                cache_file, rotation = page_specs[page_idx]
                try:
                    fpath = build_output_name(base, ext, group_count, gi, seq)
                    if ext in ("jpg", "jpeg") and not rotation:
                        shutil.copy2(cache_file, fpath)
                    else:
                        with Image.open(cache_file) as src:
                            img = src.rotate(rotation, expand=True) if rotation else src.copy()
                        _write_image(img, fpath, ext)
                        img.close()
                    saved_files.append(fpath)
                except Exception as e:
                    failed_pages += 1
                    logger.warning("保存页失败 [%s]: %s", cache_file, e)

    # 一页都没存下来时必须报错：否则调用方（GUI）会"提示成功但产出 0 个文件"
    if not saved_files and page_specs:
        raise RuntimeError(
            f"没有成功保存任何页面（共 {len(page_specs)} 页，失败 {failed_pages} 页）")

    _finalize(saved_files, job_id, extra_dirs, device_name, device_ip,
              len(page_specs), ext)
    return saved_files
