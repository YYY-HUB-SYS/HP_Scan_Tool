"""
CLI 命令行模式 — argparse 子命令入口
支持: scan / discover / status / history
复用 escl_engine / wia_engine / history_manager 核心函数
"""

import argparse
import json
import os
import sys
from datetime import datetime

# 确保能导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from escl_engine import (
    discover_scanners, probe_escl, fetch_capabilities,
    get_scanner_status, execute_scan, ScannerInfo,
)
from wsd_engine import discover_all_scanners
import history_manager


def cmd_scan(args):
    """执行扫描并保存文件"""
    ip = args.ip
    resolution = args.resolution
    fmt = args.format
    source = args.source
    output = args.output or os.path.join(os.path.expanduser("~"), "Documents", "HP_Scans")

    # 探测 eSCL
    print(f"正在探测 {ip} 的 eSCL 服务...")
    escl_url = probe_escl(ip, timeout=5.0)
    if not escl_url:
        escl_url = probe_escl(ip, port=443, timeout=5.0)
    if not escl_url:
        print(f"错误: 无法在 {ip} 上找到 eSCL 服务", file=sys.stderr)
        return 1

    # 构建 ScannerInfo
    scanner = ScannerInfo(name=f"CLI-{ip}", ip=ip, escl_url=escl_url)
    scanner = fetch_capabilities(scanner, timeout=5.0)

    # 确定颜色模式
    color_mode = "RGB24"
    if args.color == "gray":
        color_mode = "Grayscale8"
    elif args.color == "bw":
        color_mode = "BlackAndWhite1"

    # 执行扫描
    print(f"正在扫描 (DPI={resolution}, 格式={fmt}, 来源={source})...")
    data, ext = execute_scan(
        scanner=scanner,
        resolution=resolution,
        color_mode=color_mode,
        output_format=fmt,
        source=source,
        duplex=args.duplex,
        timeout=90.0,
    )

    # 保存文件
    os.makedirs(output, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"HP_CLI_{ts}.{ext}"
    fpath = os.path.join(output, fname)

    with open(fpath, "wb") as f:
        f.write(data)

    print(f"已保存: {fpath}")

    # 记录历史
    history_manager.append({
        "device_name": scanner.model or f"CLI-{ip}",
        "device_ip": ip,
        "page_count": 1,
        "format": ext,
        "file_path": fpath,
        "source": "cli",
        "resolution": resolution,
        "color_mode": color_mode,
        "output_format": fmt,
        "scan_source": source,
    })

    return 0


def cmd_discover(args):
    """发现局域网扫描仪"""
    timeout = args.timeout
    fmt = args.format

    print(f"正在发现扫描仪 (超时={timeout}s)...")

    if args.wsd_only:
        from wsd_engine import discover_wsd
        scanners = discover_wsd(timeout=timeout)
    elif args.mdns-only:
        scanners = discover_scanners(timeout=timeout)
    else:
        scanners = discover_all_scanners(timeout=timeout)

    if fmt == "json":
        result = []
        for s in scanners:
            result.append({
                "name": s.name,
                "ip": s.ip,
                "model": s.model,
                "escl_url": s.escl_url,
                "has_adf": s.has_adf,
                "has_duplex": s.has_duplex,
                "resolutions": s.resolutions,
                "color_modes": s.color_modes,
                "formats": s.formats,
            })
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not scanners:
            print("未发现任何扫描仪")
        else:
            print(f"发现 {len(scanners)} 台扫描仪:")
            for i, s in enumerate(scanners, 1):
                print(f"  {i}. {s.display_name} ({s.ip})")
                if s.escl_url:
                    print(f"     eSCL: {s.escl_url}")
                if s.has_adf:
                    print(f"     ADF: 是")
                if s.resolutions:
                    print(f"     DPI: {s.resolutions}")

    return 0


def cmd_status(args):
    """查询扫描仪状态"""
    ip = args.ip
    fmt = args.format

    escl_url = probe_escl(ip, timeout=5.0)
    if not escl_url:
        escl_url = probe_escl(ip, port=443, timeout=5.0)

    if not escl_url:
        if fmt == "json":
            print(json.dumps({"ip": ip, "status": "offline", "escl": False}))
        else:
            print(f"{ip}: 离线或无 eSCL 服务")
        return 1

    try:
        status = get_scanner_status(escl_url, timeout=5.0)
        if fmt == "json":
            status["ip"] = ip
            status["escl_url"] = escl_url
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print(f"{ip}: 在线")
            print(f"  eSCL: {escl_url}")
            if "adf_loaded" in status:
                print(f"  ADF 有纸: {'是' if status['adf_loaded'] else '否'}")
            if "state" in status:
                print(f"  状态: {status['state']}")
    except Exception as e:
        if fmt == "json":
            print(json.dumps({"ip": ip, "escl_url": escl_url, "error": str(e)}))
        else:
            print(f"{ip}: 状态查询失败 — {e}")
        return 1

    return 0


def cmd_history(args):
    """查看扫描历史"""
    limit = args.limit
    fmt = args.format

    records = history_manager.list_records(limit=limit)

    if fmt == "json":
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        if not records:
            print("无扫描历史")
        else:
            print(f"最近 {len(records)} 条扫描记录:")
            for i, r in enumerate(records, 1):
                ts = r.get("timestamp", "")[:19].replace("T", " ")
                device = r.get("device_name", r.get("device_ip", "?"))
                pages = r.get("page_count", 1)
                fmt_val = r.get("format", "?").upper()
                path = r.get("file_path", "")
                print(f"  {i}. [{ts}] {device} | {pages}页 | {fmt_val} | {path}")

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hp_scan",
        description="惠普集成扫描工具 v4.0 — CLI 模式",
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # scan 子命令
    p_scan = subparsers.add_parser("scan", help="执行扫描")
    p_scan.add_argument("--ip", required=True, help="打印机 IP 地址")
    p_scan.add_argument("--resolution", type=int, default=300, help="分辨率 DPI (默认 300)")
    p_scan.add_argument("--format", default="jpg", choices=["jpg", "png", "pdf", "bmp"],
                        help="输出格式 (默认 jpg)")
    p_scan.add_argument("--color", default="color", choices=["color", "gray", "bw"],
                        help="颜色模式 (默认 color)")
    p_scan.add_argument("--source", default="Platen", choices=["Platen", "Feeder"],
                        help="扫描来源 (默认 Platen)")
    p_scan.add_argument("--duplex", action="store_true", help="启用双面扫描 (仅 ADF 支持)")
    p_scan.add_argument("--output", help="输出目录 (默认 ~/Documents/HP_Scans)")

    # discover 子命令
    p_discover = subparsers.add_parser("discover", help="发现局域网扫描仪")
    p_discover.add_argument("--timeout", type=float, default=4.0, help="发现超时秒数 (默认 4.0)")
    p_discover.add_argument("--format", default="text", choices=["json", "text"],
                            help="输出格式 (默认 text)")
    p_discover.add_argument("--wsd-only", action="store_true", help="仅使用 WSD 发现")
    p_discover.add_argument("--mdns-only", action="store_true", help="仅使用 mDNS 发现")

    # status 子命令
    p_status = subparsers.add_parser("status", help="查询扫描仪状态")
    p_status.add_argument("--ip", required=True, help="打印机 IP 地址")
    p_status.add_argument("--format", default="text", choices=["json", "text"],
                          help="输出格式 (默认 text)")

    # history 子命令
    p_history = subparsers.add_parser("history", help="查看扫描历史")
    p_history.add_argument("--limit", type=int, default=20, help="显示条数 (默认 20)")
    p_history.add_argument("--format", default="text", choices=["json", "text"],
                           help="输出格式 (默认 text)")

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    cmd_map = {
        "scan": cmd_scan,
        "discover": cmd_discover,
        "status": cmd_status,
        "history": cmd_history,
    }

    return cmd_map[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
