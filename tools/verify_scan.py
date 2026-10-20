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
    3  实际页数与 --pages 期望值不符，或扫描过程异常未取到页

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


def main():
    ap = argparse.ArgumentParser(description="HP Scan Tool 真机验证")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--pages", type=int, default=1,
                    help="期望页数：扫描后校验实际页数，不符则退出码 3")
    ap.add_argument("--out", required=True)
    ap.add_argument("--resolution", type=int, default=300)
    ap.add_argument("--source", default="Platen", choices=["Platen", "Feeder"])
    args = ap.parse_args()

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
    ext = "jpg"
    scan_error = None
    try:
        if args.source == "Feeder":
            pages = execute_multipage_scan(
                scanner=scanner, resolution=args.resolution, color_mode="RGB24",
                output_format="jpg", source="Feeder", duplex=False, timeout=300.0)
            for i, (data, page_ext) in enumerate(pages, 1):
                ext = page_ext
                p = cache_manager.write_page(job_id, i, data, page_ext)
                cached.append(p)
                _a4_crop(p, args.resolution)
        else:
            data, ext = execute_scan(
                scanner=scanner, resolution=args.resolution, color_mode="RGB24",
                output_format="jpg", source="Platen", duplex=False, timeout=120.0)
            p = cache_manager.write_page(job_id, 1, data, ext)
            cached.append(p)
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
    saved = []
    for i, cpath in enumerate(cached, 1):
        target = "%s_%03d.%s" % (base, i, ext)
        img = Image.open(cpath)
        img.convert("RGB").save(target, "JPEG", quality=95)
        saved.append(target)

    print("[4/5] 采集元数据...")
    from PIL import Image
    page_meta = []
    for i, cpath in enumerate(cached, 1):
        img = Image.open(cpath)
        page_meta.append({
            "index": i,
            "cache_file": os.path.basename(cpath),
            "width": img.width,
            "height": img.height,
            "format": img.format,
        })
    for i, spath in enumerate(saved, 1):
        page_meta[i - 1]["saved_file"] = os.path.basename(spath)
        page_meta[i - 1]["saved_bytes"] = os.path.getsize(spath)

    print("[5/5] 清理缓存并写报告...")
    cache_manager.remove_job(job_id)

    report = {
        "device": {"ip": args.ip, "model": scanner.model, "escl_url": url,
                   "has_adf": scanner.has_adf},
        "params": {"resolution": args.resolution, "source": args.source,
                   "requested_pages": args.pages},
        "result": {"page_count": len(cached), "ext": ext,
                   "elapsed_sec": round(elapsed, 1)},
        "pages": page_meta,
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
    if actual != args.pages or scan_error is not None:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
