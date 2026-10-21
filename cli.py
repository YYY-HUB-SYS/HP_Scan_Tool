"""
CLI 命令行模式 — argparse 子命令入口
支持: scan / discover / status / history
复用 escl_engine / wia_engine / history_manager 核心函数
"""

import argparse
import io
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

import naming
from escl_engine import (
    discover_scanners, probe_escl, fetch_capabilities,
    get_scanner_status, execute_scan, ScannerInfo,
    REQUEST_FORMAT, ScanCancelled, ScanDeviceError,
)
from wsd_engine import discover_all_scanners
import history_manager
import save_manager
# 曝光预设：preset_manager 是**预设名的唯一真源**（内置 + 用户自定义），
# exposure 是**像素处理引擎**。两个模块都不碰 Qt ⇒ CLI 依赖链仍然纯控制台。
import exposure
import preset_manager

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


def _json_dumps(payload) -> str:
    """
    把 payload 序列化成 **纯 ASCII** 的 JSON 文本（`--format json` 的**唯一出口**）。

    ★ 为什么必须纯 ASCII（终审复审 I-3 · Important）：
      Windows 上 `sys.stdout` 用 **locale 编码**（本机 zh-CN ⇒ cp936/GBK）。
      stdout 被**管道 / 重定向到文件**时，Python 按 locale 编码写字节
      ⇒ 含中文的 JSON 落成 **GBK 字节** ⇒ 消费方按 UTF-8 读
      （`json.loads(stdout_bytes)` / `jq` / `Get-Content -Encoding utf8`）
      直接 UnicodeDecodeError —— 而 RFC 8259 规定 JSON 文本用 UTF-8。
      实测（终审报告）：`cli.py history --format json` ⇒ 1775 字节 GBK，
      `json.loads(stdout_bytes)` 在第 422 字节抛 UnicodeDecodeError。

      `ensure_ascii=True`（json 模块的**默认值**）把非 ASCII 字符转成
      `\\uXXXX` 转义 ⇒ 输出**纯 ASCII** ⇒ GBK / UTF-8 / Latin-1 下
      **字节完全一致** ⇒ 任何代码页下都能 `json.loads` 成功。
      （JSON 标准允许转义：`"\\u4f60\\u597d"` 与 `"你好"` 完全等价。）

    ★ 为什么**不**去改 stdout 的编码（`sys.stdout.reconfigure(encoding="utf-8")`）：
      那会改掉**所有**输出（含人类可读文案）的编码 —— 交互式控制台
      （cp936）里中文会变成乱码，等于把「消费方解析失败」升级成
      「用户眼前的界面坏了」。纯 ASCII 的方案两边都不误伤。

    ★ 缩进固定 2：JSON 消费方不在乎缩进，人工排查时更易读。
    """
    return json.dumps(payload, ensure_ascii=True, indent=2)


def _non_negative_int(value: str) -> int:
    """
    `--limit` 的 argparse 类型校验：只接受 **>= 0** 的整数。

    负数必须**明确报错**而不是静默改语义：
    `history_manager.list_records` 用 `records[-limit:]`
    ⇒ `limit=-1` 时 `records[--1:]` 等于「除最旧一条外的全部」
    ⇒ 这种输出没人能猜到 ⇒ 直接拒绝（argparse 退出码 2 + stderr 提示）。
    """
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("条数必须是整数，收到 %r" % (value,))
    if limit < 0:
        raise argparse.ArgumentTypeError(
            "条数不能为负数（收到 %d）；0 表示不显示任何记录" % limit)
    return limit


# ── 曝光预设（--exposure）─────────────────────────────────────────────
#
# ★ 背景：preset_manager / exposure 已在 2026-10-20 并回本项目，
#   save_manager 的落盘链路也支持曝光（顺序：曝光 → 旋转 → 纠偏 → A4 归一），
#   但 CLI 的 `scan` 子命令**压根没有曝光参数** ⇒ 这条入口用不上。
#
# ★ 预设名是**唯一真源 = preset_manager**（内置 4 个 + %APPDATA% 下的用户
#   自定义预设）⇒ CLI 不自己维护一份名字表 ⇒ 不会与 GUI 漂移。

#: 曝光参数字典的字段（exposure.apply_exposure / save_manager 认的键）
_EXPOSURE_PARAM_KEYS = ("mode", "brightness", "contrast", "gamma",
                        "shadows", "highlights", "channel_gains")

#: 曝光参数的默认值（预设条目缺字段时用）
_EXPOSURE_PARAM_DEFAULTS = {
    "mode": "off",
    "brightness": 0,
    "contrast": 0,
    "gamma": 1.0,
    "shadows": 0,
    "highlights": 0,
    "channel_gains": (1.0, 1.0, 1.0),
}


def _builtin_exposure_names() -> list:
    """内置曝光预设名（唯一真源 = preset_manager.builtin_presets）。"""
    names = []
    for preset in preset_manager.builtin_presets():
        name = preset.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _exposure_params_from_preset(preset: dict) -> dict:
    """
    把预设条目转成曝光参数字典（交给 apply_exposure / save_manager）。

    ★ 只挑**已知字段**：预设 JSON 由用户编辑，可能带 "name" / "builtin" 之类
      的额外键 ⇒ 直接 `apply_exposure(**preset)` 会因未知关键字炸掉。
    """
    params = dict(_EXPOSURE_PARAM_DEFAULTS)
    for key in _EXPOSURE_PARAM_KEYS:
        if key in preset:
            params[key] = preset[key]
    return params


def _resolve_exposure_preset(name: str):
    """
    按名字找曝光预设。

    返回 (预设条目 | None, 全部可用预设名列表)。
    找不到 ⇒ 第一项为 None ⇒ 调用方必须**明确报错退出**（不许静默忽略）。
    """
    found = None
    available = []
    for preset in preset_manager.load_presets():
        pname = preset.get("name") if isinstance(preset, dict) else None
        if not isinstance(pname, str) or not pname:
            continue
        available.append(pname)
        if pname == name and found is None:
            found = preset
    return found, available


def _apply_exposure_bytes(data: bytes, ext: str, params: dict) -> bytes:
    """
    对图片字节应用曝光，返回**同格式**的新字节。

    ★ 只改像素、**不改几何**（不缩放、不加 A4 白边）⇒ CLI 原有的
      「非 PDF 字节原样落盘」契约在指定曝光时只多出「像素被处理过」这一项
      变化（尺寸 / 格式 / 扩展名都不变）。
    ★ PDF 不走这里：PDF 由 save_manager 合成（那边本来就支持曝光）。
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as src:
        img = src.copy()
    out = None
    converted = None
    try:
        out = exposure.apply_exposure(img, **params)
        if out.mode not in ("RGB", "L"):
            converted = out.convert("RGB")
            target = converted
        else:
            target = out
        buf = io.BytesIO()
        if ext == "png":
            target.save(buf, "PNG")
        elif ext in ("tiff", "tif"):
            target.save(buf, "TIFF")
        else:
            target.save(buf, "JPEG", quality=95)
        return buf.getvalue()
    finally:
        for im in (converted, out):
            if im is not None and im is not img:
                im.close()
        img.close()


def _write_pdf_via_save_manager(data: bytes, fpath: str,
                                device_name: str = "", device_ip: str = "",
                                exposure: dict | None = None) -> str:
    """
    把扫描到的**图片字节**合成为 PDF 并落盘，返回落盘路径。

    打印机只出图片、PDF 由本地合成（见 escl_engine.REQUEST_FORMAT）——
    所以 CLI 不能把打印机返回的字节直接写进 .pdf，那会产出「打不开的损坏文件」
    （历史回归：CLI 打印「已保存」、退出码 0、无任何告警）。

    合成复用 save_manager 的合成器（save_single → _write_pdf → A4 画布），
    与 GUI 走同一份代码，两边的 PDF 规格不会漂移。

    CLI 手里只有内存里的字节、没有缓存页文件，故先落一个临时缓存页交给
    save_manager 处理；临时目录用完即删（不留中间产物）。

    exposure: 曝光参数字典（None = 不做曝光）。**直接交给 save_manager**
    ⇒ 曝光在「曝光 → 旋转 → 纠偏 → A4 归一」的既定顺序里生效，
    与 GUI 完全一致。
    """
    tmp_dir = tempfile.mkdtemp(prefix="hp_scan_cli_")
    try:
        cache_file = os.path.join(tmp_dir, "page_001.jpg")
        with open(cache_file, "wb") as f:
            f.write(data)
        save_manager.save_single(cache_file, "pdf", fpath,
                                 device_name=device_name, device_ip=device_ip,
                                 exposure=exposure)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return fpath


def _timestamped_name(prefix: str, ext: str) -> str:
    """
    生成「时间戳 + 随机唯一成分」的输出文件名。

    格式：`{prefix}_{YYYYmmdd}_{HHMMSS}_{唯一成分}.{ext}`
    （唯一成分 = **1 位字母（a–p）+ 7 位十六进制**，见 `naming` 模块）

    ★ 时间戳是**秒级**的（2026-10-21 统一形态）：
      本机 Windows 的**系统时钟粒度是 15.625 ms**
      （`time.get_clock_info('time').resolution` 实测 0.015625）
      ⇒ 同一 tick 内两次取名的 `%f` **99.5% 完全相同**
      ⇒ 之前那 6 位微秒**没有额外信息量** ⇒ 留着是**虚假精度** ⇒ 已下线。
      同一秒内完成的两批 CLI 扫描若算出**同一个文件名**
      ⇒ 后写的把先写的整个覆盖掉 ⇒ 先写的那批产物**静默消失**
      （用户看不到、也没有任何报错）—— 这靠**随机成分**解决，不靠时钟。
    ★ 唯一性来自 `uuid4()`（`naming.unique_token`）
      ⇒ **不依赖时钟分辨率**：同一 tick / 同一秒内都必然不同。
    ★ 唯一成分**首字符是字母** ⇒ 永远不会被误读成分组编号（`_001`）。
    ★ 不用「撞名加 -1/-2 序号」：产物名已有**分组编号**规则
      （`{base}_{组号}_{组内号}.ext` / `{base}_{N:03d}.ext`）
      ⇒ 再加序号会与组号混淆。
    ★ 时间戳仍在**前面** ⇒ 人一眼能看出大致何时产生（可读性不丢）。
    ★ CLI 侧**没有** GUI 那层 `_OutputNameGuard` 兜底：CLI 自己造名、
      自己落盘（`save_manager` 落盘是覆盖写）⇒ 这个唯一性是**必需**的。
    ★ 形态与协调器 / GUI **共用** `naming` 模块的**同一份**实现
      （三处不能互相 import ⇒ 收敛到零依赖模块）。
    """
    return naming.build_output_name(prefix, ext, now=datetime.now())


def cmd_scan(args):
    """执行扫描并保存文件"""
    ip = args.ip
    resolution = args.resolution
    fmt = args.format
    source = args.source
    output = args.output or os.path.join(os.path.expanduser("~"), "Documents", "HP_Scans")

    # ── 曝光预设解析（**先于任何网络动作**）────────────────────────
    # 预设不存在 ⇒ **立刻**报错退出：不扫描、不落盘、不吐 traceback。
    # （早失败 = 用户不用干等一次真实扫描才发现预设名打错了。）
    # 用 getattr 而不是 args.exposure：手工构造的 Namespace（老调用方 /
    # 外部脚本）没有这个键时不该炸 —— 缺省即"不做曝光"。
    exposure_params = None
    exposure_name = getattr(args, "exposure", None)
    if exposure_name:
        try:
            preset, available = _resolve_exposure_preset(exposure_name)
        except Exception as e:
            print(f"错误: 读取曝光预设失败 — {e}", file=sys.stderr)
            return 1
        if preset is None:
            print(f"错误: 曝光预设不存在: {exposure_name}", file=sys.stderr)
            if available:
                print("可用曝光预设: " + "、".join(available), file=sys.stderr)
            else:
                print("可用曝光预设: （无）", file=sys.stderr)
            return 2
        exposure_params = _exposure_params_from_preset(preset)

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
    exposure_hint = f", 曝光={exposure_name}" if exposure_name else ""
    print(f"正在扫描 (DPI={resolution}, 格式={fmt}, 来源={source}"
          f"{exposure_hint})...", file=sys.stderr)
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
    # ★ 产物名唯一（复审 I1 → 2026-10-21 统一形态）：CLI 自己造名、不走协调器
    #   ⇒ 名字里除秒级时间戳外还带一段**随机唯一成分**
    #   （`naming.unique_token()`：1 位字母 + 7 位十六进制，来自 uuid4）。
    #   只靠时间戳是不够的：本机系统时钟粒度 15.625 ms ⇒ 同一 tick 内两次
    #   取名的时间戳完全相同 ⇒ 后写的会覆盖先写的（产物静默消失）。
    #   CLI 侧没有 GUI 的 `_OutputNameGuard` 兜底 ⇒ 这里必须自己保证唯一。
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
                                        device_name=device_name, device_ip=ip,
                                        exposure=exposure_params)
        except Exception as e:
            print(f"错误: PDF 合成失败 — {e}", file=sys.stderr)
            return 1
        history_recorded = True
    else:
        # 非 PDF（jpg/png/tiff）：字节原样写入，行为与改造前完全一致。
        # 指定了曝光 ⇒ 先在**字节层**应用曝光（只改像素，不改几何）。
        if exposure_params is not None:
            try:
                data = _apply_exposure_bytes(data, ext, exposure_params)
            except Exception as e:
                print(f"错误: 曝光处理失败 — {e}", file=sys.stderr)
                return 1
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
        record = {
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
        }
        # ★ 只有**真的用了**曝光才加这个键 —— 不指定 --exposure 时
        #   历史记录必须与改造前**逐字段一致**（向后兼容）。
        if exposure_params is not None:
            record["exposure"] = exposure_params
        history_manager.append(record)

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
        print(_json_dumps(result))
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
            print(_json_dumps({"ip": ip, "status": "offline", "escl": False}))
        else:
            print(f"{ip}: 离线或无 eSCL 服务")
        return 1

    try:
        status = get_scanner_status(escl_url, timeout=5.0)
        if fmt == "json":
            status["ip"] = ip
            status["escl_url"] = escl_url
            print(_json_dumps(status))
        else:
            print(f"{ip}: 在线")
            print(f"  eSCL: {escl_url}")
            if "adf_loaded" in status:
                print(f"  ADF 有纸: {'是' if status['adf_loaded'] else '否'}")
            if "state" in status:
                print(f"  状态: {status['state']}")
    except Exception as e:
        if fmt == "json":
            print(_json_dumps({"ip": ip, "escl_url": escl_url, "error": str(e)}))
        else:
            print(f"{ip}: 状态查询失败 — {e}")
        return 1

    return 0


def cmd_history(args):
    """查看扫描历史"""
    limit = args.limit
    fmt = args.format

    # ★ `--limit` 的条数语义（终审复审：这是真 bug）：
    #   - 负数 ⇒ **拒绝**（`records[--1:]` 是合法 Python，语义诡异
    #     ⇒ 等于「除最旧一条外的全部」，没人能猜到 ⇒ 不允许静默）；
    #   - 0 ⇒ **0 条**（不是「全部」！）：`history_manager.list_records` 用
    #     `records[-limit:]`，`limit=0` 时 `records[-0:]` 等于**全量**
    #     ⇒ 直接透传会把全部记录打出来 ⇒ 这里在 CLI 侧拦成空列表。
    #     （`history_manager` 不在本次改动范围 ⇒ 修在调用方，更小更安全。）
    if limit < 0:
        print("错误: --limit 不能为负数（收到 %d）；0 表示不显示任何记录" % limit,
              file=sys.stderr)
        return 2
    records = history_manager.list_records(limit=limit) if limit > 0 else []

    if fmt == "json":
        print(_json_dumps(records))
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
    # --exposure：曝光预设名。预设名的唯一真源是 preset_manager
    # （内置 4 个 + %APPDATA%\HP_Scan_Tool\presets.json 里的用户自定义预设），
    # 所以 help 里的内置名单是**运行时从 preset_manager 取的** ⇒ 不会与 GUI 漂移。
    # ★ help 文本里的 % 必须写成 %%：argparse 用 % 格式化 help 串
    #   （_expand_help）⇒ 单个 % 会抛 ValueError（unsupported format character）。
    p_scan.add_argument(
        "--exposure",
        help="曝光预设名（可选）。内置预设: "
             + "、".join(_builtin_exposure_names())
             + "；用户自定义预设见 %%APPDATA%%\\HP_Scan_Tool\\presets.json。"
               "不指定则不做曝光处理（默认，产物与不加曝光时一致）。"
               "预设名不存在会直接报错退出并列出可用预设")

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
    p_history.add_argument("--limit", type=_non_negative_int, default=20,
                            help="显示条数 (默认 20；0 表示不显示任何记录；"
                                 "负数会被直接拒绝)")
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
