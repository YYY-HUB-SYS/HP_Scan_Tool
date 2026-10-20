"""
真机扫描验证脚本 — HP Scan Tool
用途：改造前（D1）与改造后（D2）跑同一流程，产出可对照的 report.json。

用法:
    python tools/verify_scan.py --ip <IP> --pages 1 --out .verify_out/D1_platen
    python tools/verify_scan.py --ip <IP> --pages 3 --out .verify_out/D1_adf

--pages 是「期望页数」：扫描完成后校验实际抓到的页数，不符则以退出码 3 结束
（不做截断，以免掩盖打印机实际出了几页）。

退出码:
    0  扫描成功，且实际页数与 --pages 期望值一致
    2  探测不到 eSCL 服务（设备不可达）
    3  实际页数与 --pages 期望值不符，或扫描过程异常未取到页，
       或缓存页解码失败（设备返回的不是可解码图像）

★ 落盘文件名与内容格式必须一致：文件名按**该页实际解码出来的格式**命名，
  保存也用同一格式。旧实现按「最后一页声明的 ext」命名、内容恒为 JPEG ⇒
  多页且各页 ext 不一致时会出现「.png 文件里装的是 JPEG」。

★ 失败路径与「页数不符」走同一条路：退出码 3 + 写 report.json（记录失败
  原因）+ 清理缓存目录，绝不留孤儿缓存、绝不抛未捕获异常。

注意：会真的让打印机出纸。
"""

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cache_manager
from escl_engine import (
    fetch_capabilities, execute_scan, execute_multipage_scan,
    probe_escl, crop_to_a4,
)

# 缓存页的**实际**格式 → (PIL 保存格式, 文件扩展名)。
# 落盘文件名与内容格式由这张表绑定，杜绝「.png 里装 JPEG」。
_SAVE_FORMAT = {
    "JPEG": ("JPEG", "jpg"),
    "PNG": ("PNG", "png"),
    "TIFF": ("TIFF", "tiff"),
    "BMP": ("BMP", "bmp"),
    "GIF": ("GIF", "gif"),
    "WEBP": ("WEBP", "webp"),
}


def _a4_crop(path, dpi):
    """与 GUI 的 _apply_post_scan_fixups 行为一致"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        cropped = crop_to_a4(img, dpi=dpi)
        if cropped.size != img.size:
            buf = io.BytesIO()
            cropped.convert("RGB").save(buf, format="JPEG")
            with open(path, "wb") as f:
                f.write(buf.getvalue())
    except Exception as e:
        print("  [warn] A4 裁剪失败: %s" % e)


def _probe_size(path):
    """
    读出文件的像素尺寸（**只读**，不修改文件）。失败返回 None。

    ★ 为什么需要它：`_a4_crop` 是**原地覆写**（把缓存页改成 A4 画布），
      覆写之后再量就只剩裁剪后的尺寸 ⇒ 裁剪前的原始扫描幅面
      （设备 MaxWidth×MaxHeight）在磁盘上根本不存在。
      本函数在裁剪**之前**调一次，把原始幅面记进 report.json，
      于是 report.json 同时给出「裁剪前 / 裁剪后」两组尺寸。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as img:
            return img.width, img.height
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="HP Scan Tool 真机验证")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--pages", type=int, default=1,
                    help="期望页数：扫描后校验实际页数，不符则退出码 3")
    ap.add_argument("--out", required=True)
    ap.add_argument("--resolution", type=int, default=300)
    ap.add_argument("--source", default="Platen", choices=["Platen", "Feeder"])
    ap.add_argument("--no-a4-crop", action="store_true",
                    help="跳过 A4 归一（原样保留设备原始扫描幅面）。"
                         "默认行为不变（仍然原地裁剪成 A4 画布）")
    args = ap.parse_args()

    a4_crop = not args.no_a4_crop
    os.makedirs(args.out, exist_ok=True)

    print("[1/5] 探测 eSCL ...")
    url = probe_escl(args.ip, timeout=5.0)
    if not url:
        print("ERROR: 无法探测 eSCL 服务")
        return 2
    print("  eSCL = %s" % url)

    from escl_engine import ScannerInfo
    scanner = ScannerInfo(name="verify", ip=args.ip, escl_url=url)
    scanner = fetch_capabilities(scanner, timeout=5.0)
    print("  型号 = %s  ADF = %s" % (scanner.model, scanner.has_adf))

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    cache_manager.register_job(job_id)

    print("[2/5] 扫描中（打印机将出纸）...")
    t0 = time.time()
    cached = []
    declared_exts = []   # 设备声明的每页 ext
    raw_sizes = {}       # 页序号 → 裁剪**前**的像素尺寸（原始扫描幅面）
    ext = "jpg"
    scan_error = None
    try:
        if args.source == "Feeder":
            pages = execute_multipage_scan(
                scanner=scanner, resolution=args.resolution, color_mode="RGB24",
                output_format="jpg", source="Feeder", duplex=False, timeout=300.0)
            for i, (data, page_ext) in enumerate(pages, 1):
                ext = page_ext
                declared_exts.append(page_ext)
                p = cache_manager.write_page(job_id, i, data, page_ext)
                cached.append(p)
                raw_sizes[i] = _probe_size(p)      # ← 裁剪前（原始幅面）
                if a4_crop:
                    _a4_crop(p, args.resolution)
        else:
            data, ext = execute_scan(
                scanner=scanner, resolution=args.resolution, color_mode="RGB24",
                output_format="jpg", source="Platen", duplex=False, timeout=120.0)
            declared_exts.append(ext)
            p = cache_manager.write_page(job_id, 1, data, ext)
            cached.append(p)
            raw_sizes[1] = _probe_size(p)          # ← 裁剪前（原始幅面）
            if a4_crop:
                _a4_crop(p, args.resolution)
    except Exception as e:
        scan_error = e
    elapsed = time.time() - t0
    if scan_error is not None:
        print("  扫描异常：%s（耗时 %.1fs，取到 %d 页）" % (scan_error, elapsed, len(cached)))
    else:
        print("  完成，耗时 %.1fs，%d 页" % (elapsed, len(cached)))

    print("[3/5] 落盘...")
    from PIL import Image
    base = os.path.join(args.out, "HP_Scan_%s" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    saved = {}          # 页序号 → 落盘路径
    saved_exts = []     # 落盘文件实际用的扩展名（与内容格式一致）
    page_error = None
    for i, cpath in enumerate(cached, 1):
        try:
            with Image.open(cpath) as img:
                fmt = (img.format or "JPEG").upper()
                pil_fmt, save_ext = _SAVE_FORMAT.get(fmt, ("JPEG", "jpg"))
                target = "%s_%03d.%s" % (base, i, save_ext)
                kwargs = {"quality": 95} if pil_fmt == "JPEG" else {}
                img.convert("RGB").save(target, pil_fmt, **kwargs)
        except Exception as e:
            # 损坏页：与「页数不符」同一条失败路径（退出码 3），
            # 不抛未捕获异常（那会让脚本退出码变成 1，且 report.json 丢失）
            page_error = e
            print("ERROR: 第 %d 页解码失败: %s" % (i, e), file=sys.stderr)
            break
        saved[i] = target
        saved_exts.append(save_ext)

    print("[4/5] 采集元数据...")
    page_meta = []
    for i, cpath in enumerate(cached, 1):
        entry = {"index": i, "cache_file": os.path.basename(cpath)}
        # 裁剪**前**（原始扫描幅面）—— 与下面的 width/height（裁剪后）成对，
        # 让 report.json 同时给出两套口径，避免"只量到 A4 画布"。
        raw_size = raw_sizes.get(i)
        if raw_size:
            entry["raw_width"] = raw_size[0]
            entry["raw_height"] = raw_size[1]
        try:
            with Image.open(cpath) as img:
                entry["width"] = img.width
                entry["height"] = img.height
                entry["format"] = img.format
        except Exception as e:
            entry["error"] = str(e)
            if page_error is None:
                page_error = e
                print("ERROR: 第 %d 页元数据读取失败: %s" % (i, e), file=sys.stderr)
        page_meta.append(entry)
    for i, spath in saved.items():
        page_meta[i - 1]["saved_file"] = os.path.basename(spath)
        page_meta[i - 1]["saved_bytes"] = os.path.getsize(spath)

    print("[5/5] 清理缓存并写报告...")
    try:
        cache_manager.remove_job(job_id)
    except Exception as e:
        print("  [warn] 缓存清理失败: %s" % e, file=sys.stderr)

    failures = []
    if scan_error is not None:
        failures.append({"stage": "scan", "error": str(scan_error)})
    if page_error is not None:
        failures.append({"stage": "decode", "error": str(page_error)})

    if saved_exts:
        result_ext = saved_exts[0] if len(set(saved_exts)) == 1 else "mixed"
    else:
        result_ext = declared_exts[-1] if declared_exts else "jpg"

    report = {
        "device": {"ip": args.ip, "model": scanner.model, "escl_url": url,
                   "has_adf": scanner.has_adf},
        "params": {"resolution": args.resolution, "source": args.source,
                   "requested_pages": args.pages,
                   "a4_crop": a4_crop},
        # ★ 两套口径（2026-10-21 订正）：`_a4_crop` 是**原地覆写**，
        #   裁剪前的原始扫描幅面在磁盘上不存在 ⇒ 必须靠 raw_* 字段留底。
        "size_basis": {
            "raw": "pages[].raw_width/raw_height = 裁剪前（设备原始扫描幅面）",
            "cropped": "pages[].width/height = 裁剪后（A4 画布）",
            "a4_crop_applied": a4_crop,
        },
        "result": {"page_count": len(cached), "saved_page_count": len(saved),
                   "ext": result_ext, "exts": saved_exts,
                   "declared_exts": declared_exts,
                   "elapsed_sec": round(elapsed, 1)},
        "pages": page_meta,
        "failures": failures,
        "error": "; ".join(f["error"] for f in failures) or None,
    }
    report_path = os.path.join(args.out, "report.json")
    with open(report_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("报告: %s" % report_path)

    # 页数校验：--pages 是期望值，不是截断上限。
    # 实际抓到的页数全部落进 report.json（requested_pages vs result.page_count），
    # 不符时以退出码 3 报错，避免 D1/D2 投纸张数不同造成假差异。
    actual = len(cached)
    if actual != args.pages:
        print("ERROR: 页数不符 — 期望 %d 页，实际 %d 页" % (args.pages, actual), file=sys.stderr)
    if scan_error is not None:
        print("ERROR: 扫描异常：%s" % scan_error, file=sys.stderr)
    if page_error is not None:
        print("ERROR: 页面解码失败：%s" % page_error, file=sys.stderr)
    if actual != args.pages or scan_error is not None or page_error is not None:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
