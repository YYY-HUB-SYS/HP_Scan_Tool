"""
保存落盘管理器 — HP Scan Tool v4.0
把扫描结果写入目标目录：格式转换 / 分组命名 / 额外目录复制 / 缓存清理 / 历史写入。
纯 Python 实现，不依赖 Qt 与任何 GUI，可独立单元测试。
"""

import logging
import os
import shutil

from PIL import Image

import cache_manager
import history_manager

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


def _save_a4_pdf(img, path, dpi=300):
    """把图片居中放到 A4 画布上，保存为标准尺寸 PDF"""
    a4_w = int(210 / 25.4 * dpi)
    a4_h = int(297 / 25.4 * dpi)
    w, h = img.size
    if w != a4_w or h != a4_h:
        canvas = Image.new("RGB", (a4_w, a4_h), "white")
        x = (a4_w - w) // 2
        y = (a4_h - h) // 2
        if img.mode != "RGB":
            img = img.convert("RGB")
        canvas.paste(img, (x, y))
        canvas.save(path, "PDF", resolution=dpi)
    else:
        img.convert("RGB").save(path, "PDF", resolution=dpi)


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


def _finalize(saved_files, job_id, extra_dirs, device_name, device_ip,
              page_count, ext):
    """额外目录复制 + 清缓存 + 写历史"""
    copy_to_extra_dirs(saved_files, extra_dirs)
    if job_id:
        cache_manager.remove_job(job_id)
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
    with Image.open(cache_path) as src:
        img = src.rotate(rotation, expand=True) if rotation else src.copy()
        final = output_path
        if not final.lower().endswith(f".{ext}"):
            final = f"{final.rsplit('.', 1)[0]}.{ext}"
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

    for gi, group_indices in enumerate(groups, 1):
        if ext == "pdf":
            images = []
            for page_idx in group_indices:
                cache_file, rotation = page_specs[page_idx]
                try:
                    with Image.open(cache_file) as src:
                        img = src.rotate(rotation, expand=True) if rotation else src.copy()
                    images.append(img.convert("RGB"))
                except Exception as e:
                    logger.debug("读取缓存页失败 [%s]: %s", cache_file, e)
            if not images:
                continue
            pdf_path = build_output_name(base, ext, group_count, gi, 1)
            images[0].save(pdf_path, "PDF", save_all=True,
                           append_images=images[1:])
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
                    logger.debug("保存页失败 [%s]: %s", cache_file, e)

    _finalize(saved_files, job_id, extra_dirs, device_name, device_ip,
              len(page_specs), ext)
    return saved_files
