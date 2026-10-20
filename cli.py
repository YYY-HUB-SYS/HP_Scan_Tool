"""
CLI 命令行模式 — argparse 子命令入口
支持: scan / discover / status / history
复用 escl_engine / wia_engine / history_manager 核心函数
"""

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

# 确保能导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from escl_engine import (
    discover_scanners, probe_escl, fetch_capabilities,
    get_scanner_status, execute_scan, ScannerInfo,
    REQUEST_FORMAT, ScanCancelled, ScanDeviceError,
)
from wsd_engine import discover_all_scanners
import history_manager
import save_manager

logger = logging.getLogger(__name__)

# ★ CLI **不接管**全局 logging，但必须掐掉 Python 的兜底 handler。
#
# 背景（复审 N-1）：CLI 从不调用 `logging.basicConfig` ⇒ 一条日志记录如果
# 在整条 logger 链上找不到任何 handler，Python 会启用兜底 handler
# `logging.lastResort`，把它**直接写进 stderr**。而 `logger.exception(...)`
# 自带 `exc_info=True` ⇒ 记录里还带着完整 traceback（含本地绝对路径）
# ⇒ 用户在「去打印机面板取消任务 / 等 15-20 分钟」这类**可执行指引**后面
# 紧接着又看到一坨 Python 栈。
#
# 处理：给本模块的 logger 挂一个 `NullHandler`。
#   - `Logger.callHandlers` 因此统计到 handler（found >= 1）⇒ `lastResort`
#     不再启用 ⇒ 本模块的记录**不会**落 stderr；
#   - 记录仍照常向上传播给 root（谁配置了 logging 谁拿得到）⇒
#     CLI 后续要用 logging 依然可用，不是把 logging 整个关掉。
logger.addHandler(logging.NullHandler())

# 设备 / 协议类"可操作"错误的特征词。
#
# 协议层（escl_engine）对这些失败已经写好了**用户下一步该做什么**
# （409 → 去打印机面板取消任务 / 等 15-20 分钟；503 → 输稿器放纸；
#  超时 → 确认设备已开机）。CLI 必须把这段话**原样**呈现给用户，
# 而不是只甩一段 Python traceback。
#
# 判定顺序：① 异常类型是 ScanDeviceError（协议层显式标记的设备类错误）
#           ② 消息命中下面的特征词（兼容协议层仍是普通 RuntimeError 的老路径）
# 两者都不命中 ⇒ 视为"意外错误"，保留 traceback 便于排障。
_ACTIONABLE_ERROR_MARKERS = (
    "HTTP 409", "HTTP 503", "HTTP 410",
    "创建扫描任务失败", "无法获取 ScanJob URI", "不支持的输出格式",
    "扫描超时", "打印机", "输稿器", "eSCL", "ScanJob",
)


def _is_actionable_device_error(exc: BaseException) -> bool:
    """判断异常是不是"设备/协议类可操作错误"（消息里已写清用户下一步）。"""
    if isinstance(exc, ScanDeviceError):
        return True
    msg = str(exc)
    return any(marker in msg for marker in _ACTIONABLE_ERROR_MARKERS)


def _report_scan_error(exc: BaseException) -> None:
    """
    把扫描失败呈现给用户（只写 stderr，绝不写 stdout）。

    - 设备/协议类可操作错误 ⇒ stderr **只有**协议层写好的可执行指引，
      **不带 traceback**（复审 N-1：traceback 里全是本地绝对路径，
      用户看不懂也不知道下一步该干什么）；
    - 意外错误（编程错误等）⇒ stderr 给消息 **加** 一份 traceback（便于排障），
      且**只打一次**（复审 N-2：原先 `logger.exception` 与 `print_exc` 各打一份，
      同一份栈在 stderr 里连出两遍，噪声翻倍）。

    日志侧：两种情况都记一条 error 记录，但**不带 exc_info** ——
    带 exc_info 等于把 traceback 挂到记录上，而记录会传播给 root
    （外部若给 root 挂了 stderr handler，就会多出一份 traceback，
    破坏「只打一次」）。本模块的 logger 挂了 NullHandler ⇒ 记录不落 stderr，
    但外部若配置了 logging 仍能拿到这条记录。
    """
    print(f"错误: {exc}", file=sys.stderr)
    if _is_actionable_device_error(exc):
        logger.error("扫描失败（设备/协议类可操作错误）: %s", exc)
    else:
        logger.error("扫描失败（意外错误）: %s", exc)
        # 意外错误：traceback 由**这一处**负责输出（全进程只此一份）
        traceback.print_exception(type(exc), exc, exc.__traceback__,
                                  file=sys.stderr)


def _write_pdf_via_save_manager(data: bytes, fpath: str,
                                device_name: str = "", device_ip: str = "") -> str:
    """
    把扫描到的**图片字节**合成为 PDF 并落盘，返回落盘路径。

    打印机只出图片、PDF 由本地合成（见 escl_engine.REQUEST_FORMAT）——
    所以 CLI 不能把打印机返回的字节直接写进 .pdf，那会产出「打不开的损坏文件」
    （历史回归：CLI 打印「已保存」、退出码 0、无任何告警）。

    合成复用 save_manager 的合成器（save_single → _write_pdf → A4 画布），
    与 GUI 走同一份代码，两边的 PDF 规格不会漂移。

    CLI 手里只有内存里的字节、没有缓存页文件，故先落一个临时缓存页交给
    save_manager 处理；临时目录用完即删（不留中间产物）。
    """
    tmp_dir = tempfile.mkdtemp(prefix="hp_scan_cli_")
    try:
        cache_file = os.path.join(tmp_dir, "page_001.jpg")
        with open(cache_file, "wb") as f:
            f.write(data)
        save_manager.save_single(cache_file, "pdf", fpath,
                                 device_name=device_name, device_ip=device_ip)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return fpath


def _timestamped_name(prefix: str, ext: str) -> str:
    """
    生成带**微秒**精度时间戳的输出文件名。

    ★ 为什么 CLI 必须单独保证精度（复审 I1）：
      CLI 的产物名是**自己造的** —— `cli.py` 走的是 `escl_engine.execute_scan`，
      **根本不经过协调器** ⇒ 协调器那次「秒→毫秒」的改动**覆盖不到这里**。
      之前 CLI 只到秒 ⇒ 同一秒内完成的两批 CLI 扫描算出**同一个文件名**
      ⇒ 后写的把先写的整个覆盖掉 ⇒ 先写的那批产物**静默消失**
      （用户看不到、也没有任何报错）。
    ★ 命名格式与协调器保持一致（`%Y%m%d_%H%M%S_%f`），便于人工对照。
    """
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.{ext}"


def cmd_scan(args):
    """执行扫描并保存文件"""
    ip = args.ip
    resolution = args.resolution
    fmt = args.format
    source = args.source
    output = args.output or os.path.join(os.path.expanduser("~"), "Documents", "HP_Scans")

    # 探测 eSCL —— **单轮**探测，候选 = 默认(http:80 / https:443) + http:443。
    #
    # 改造前分两轮：第 1 轮 probe_escl(ip)、第 2 轮 probe_escl(ip, port=443)。
    # 第 2 轮的候选里 `https://{ip}:443` 与第 1 轮**整块重叠** ⇒ 同一个 URL
    # 被重复请求（实测 404 形态 2 次、拒连/超时形态 1 次）⇒ 每台不可达设备
    # 多 2~4 次 HTTP 尝试 + 最多约 10 秒延迟（而"不可达"恰恰是用户最需要
    # 快速拿到提示的场景）。
    # 现在用 extra_ports 把 http:443 **并入同一轮**候选表（自动去重）
    # ⇒ 零重复请求，覆盖面不缩水（http:443 设备仍能被探到）。
    print(f"正在探测 {ip} 的 eSCL 服务...", file=sys.stderr)
    escl_url = probe_escl(ip, timeout=5.0, extra_ports=(443,))
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
    # 进度提示走 stderr：stdout 留给结果（失败时 stdout 必须为空，
    # 否则消费方（如 `hp_scan scan ... > out`）会拿到半截输出）。
    print(f"正在扫描 (DPI={resolution}, 格式={fmt}, 来源={source})...", file=sys.stderr)
    try:
        data, ext = execute_scan(
            scanner=scanner,
            resolution=resolution,
            color_mode=color_mode,
            output_format=fmt,
            source=source,
            duplex=args.duplex,
            timeout=90.0,
        )
    except ScanCancelled:
        print("错误: 扫描已取消", file=sys.stderr)
        return 1
    except Exception as e:
        # F-1：协议层写好的可执行指引必须**原样**呈现给用户，
        # 不能只给一段 Python traceback（traceback 里全是本地绝对路径，
        # 用户看不懂也不知道下一步该干什么）。
        _report_scan_error(e)
        return 1

    if not data:
        print("错误: 扫描未返回数据（可能被取消）", file=sys.stderr)
        return 1

    # 保存文件
    os.makedirs(output, exist_ok=True)
    # ★ 微秒精度（复审 I1）：CLI 自己造名、不走协调器 ⇒ 精度必须在这里单独保证，
    #   否则同一秒内完成的两批会算出同一个文件名 ⇒ 后写的覆盖先写的（产物静默消失）。
    fname = _timestamped_name("HP_CLI", ext)
    fpath = os.path.join(output, fname)
    device_name = scanner.model or f"CLI-{ip}"

    # PDF 特殊处理：打印机只出图片（见 escl_engine.REQUEST_FORMAT），PDF 必须本地合成。
    # 直接把返回字节写进 .pdf 会产出「打不开的损坏文件」——合成走 save_manager
    # （与 GUI 同一份合成器），历史也由它一并记账。
    # 非 PDF（jpg/png/tiff）字节原样写入，行为与改造前完全一致。
    history_recorded = False
    if ext == "pdf" and data[:5] != b"%PDF-":
        try:
            _write_pdf_via_save_manager(data, fpath,
                                        device_name=device_name, device_ip=ip)
        except Exception as e:
            print(f"错误: PDF 合成失败 — {e}", file=sys.stderr)
            return 1
        history_recorded = True
    else:
        with open(fpath, "wb") as f:
            f.write(data)

    # 落盘校验：PDF 必须是真正的 PDF 字节（%PDF-）。
    # 绝不允许「打印已保存 + 退出码 0」却留下一个打不开的文件。
    if ext == "pdf":
        with open(fpath, "rb") as f:
            magic = f.read(5)
        if magic != b"%PDF-":
            try:
                os.remove(fpath)
            except OSError:
                pass
            print(f"错误: 生成的 PDF 非法（文件头 {magic!r}），已删除 {fpath}",
                  file=sys.stderr)
            return 1

    print(f"已保存: {fpath}")

    # 记录历史（PDF 路径已由 save_manager 记过一条，不重复记）
    if not history_recorded:
        history_manager.append({
            "device_name": device_name,
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

    # 进度提示走 stderr：--format json 时 stdout 必须是纯 JSON，
    # 否则 cli.py discover --format json | jq 之类的消费方式会解析失败。
    print(f"正在发现扫描仪 (超时={timeout}s)...", file=sys.stderr)

    if args.wsd_only:
        from wsd_engine import discover_wsd
        scanners = discover_wsd(timeout=timeout)
    elif args.mdns_only:
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

    # 探测 eSCL —— 单轮探测（与 cmd_scan 同一份候选表，见该处注释）
    escl_url = probe_escl(ip, timeout=5.0, extra_ports=(443,))

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
    # --format 的允许值 = 协议层 REQUEST_FORMAT 的键集合（唯一事实源，
    # 不会与协议层漂移）：jpg / jpeg / png / tiff / tif / pdf。
    #   - jpeg / tif / tiff 之前 CLI 不暴露（协议层与 GUI 都支持）⇒ 反向缺口；
    #   - bmp **不在** REQUEST_FORMAT 里（协议层不支持）⇒ 仍被显式拒绝，
    #     不会静默降级成别的格式。
    p_scan.add_argument("--format", default="jpg", choices=sorted(REQUEST_FORMAT),
                        help="输出格式 (默认 jpg)，可选: "
                             f"{'、'.join(sorted(REQUEST_FORMAT))}。"
                             "不支持的格式会直接报错，不会静默降级成别的格式")
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
