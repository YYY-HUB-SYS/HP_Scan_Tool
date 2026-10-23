"""
eSCL (AirScan) 扫描引擎 — 合并版
参考：
  - OpenScanHub: 多协议架构、ADF 检测
  - node-hp-scan-to: 完整 eSCL 协议交互流程(ScannerCapabilities → ScannerStatus → ScanJobs → NextDocument)
  - photo-scan-split: 极简 mDNS 发现
"""

import json
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field
from typing import Optional

import requests
import urllib3
from PIL import Image
from zeroconf import ServiceBrowser, Zeroconf, ServiceListener

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

__all__ = [
    "ScannerInfo", "discover_scanners", "probe_escl",
    "fetch_capabilities", "get_scanner_status", "probe_scanner_status",
    "execute_scan", "execute_multipage_scan",
    "auto_crop", "is_blank_page", "FORMAT_MIME", "MIME_EXT",
    "REQUEST_FORMAT", "ScanCancelled", "ScanJobCancelled", "ScanDeviceError",
    "SCAN_TIMEOUT_MESSAGE", "FIRST_PAGE_TIMEOUT_MESSAGE",
]


def _strip_ns(el) -> str:
    """剥离 XML 元素的 namespace 前缀，返回本地标签名。"""
    t = el.tag if hasattr(el, "tag") else str(el)
    return t.split("}")[-1] if "}" in t else t


# ==================== HTTP 墙钟看门狗 ====================

#: 墙钟上限相对 socket 超时的**余量**（秒）。
_WALL_CLOCK_MARGIN = 3.0


def _wall_clock_for(timeout) -> float:
    """把 requests 的 `timeout` 参数换算成**墙钟上限**（秒）。

    - `timeout=3.0` ⇒ 连接+读共 3 秒
    - `timeout=(2.0, 3.0)` ⇒ 连接 2 秒 + 读 3 秒
    再加 `_WALL_CLOCK_MARGIN` 的余量。
    """
    if isinstance(timeout, tuple):
        total = 0.0
        for part in timeout:
            try:
                total += float(part)
            except (TypeError, ValueError):
                total += 0.0
    else:
        try:
            total = float(timeout)
        except (TypeError, ValueError):
            total = 0.0
    return total + _WALL_CLOCK_MARGIN


def _http_call_bounded(call, wall_clock: float, what: str):
    """
    执行一次 HTTP 调用，并在**墙钟**上限内返回。

    ★ 为什么必须有这道墙钟（实测，2026-10-21）
    ------------------------------------------------------------------
    `requests` 的 read timeout 是 **socket 层**的。实测在「对端接受 TCP
    连接但迟迟不回数据」的状态下，这个超时**可能长时间不触发**：
    本轮排查抓到过真实样本 —— socket 读超时 3 秒，**9 分钟都没有报错**，
    调用方与对端互相阻塞（客户端在等 status line、服务端卡在 sendall）。

    ⇒ 只靠 socket 超时不够：一台"半死不活"的扫描仪就能把 GUI 的扫描线程
    **永久卡死**（用户只能杀进程）。墙钟到期后按"设备无响应"处理。

    ★ 正常路径的行为**完全不变**：socket 超时先触发 ⇒ 抛同样的异常，
    上层（`_transport_error_to_device_error` 等）看到的错误类型与文案不变。
    """
    box = {}

    def _worker():
        try:
            box["r"] = call()
        except BaseException as exc:      # noqa: BLE001 —— 原样转交调用方
            box["e"] = exc

    th = threading.Thread(target=_worker, name="hp-escl-http", daemon=True)
    th.start()
    th.join(wall_clock)
    if th.is_alive():
        raise requests.exceptions.ReadTimeout(
            "%s 在 %.1f 秒内没有返回（已放弃等待）" % (what, wall_clock))
    if "e" in box:
        raise box["e"]
    return box["r"]


# 注（2026-10-21）：这里**曾经**有一个 `_fix_jpeg_header()`
#   （"修复 HP 机型 JPEG 头高度元数据错误"），已删除。
#   原因：它比对的两边来自**同一处** SOF 段 —— PIL 的 `Image.height` 本身就是
#   从 SOF 读出来的，而它自己解析的 `header_height` 也是 SOF 的高度字段 ⇒
#   `header_height != actual_height` **恒为假** ⇒ 从未修补过任何字节。
#   实测（2026-10-21）：把 SOF 高度改成 900 后 PIL 同样报 900，
#   `_fix_jpeg_header(bad) == bad`（逐字节相同）。
#   删除它同时消除了"eSCL 路径调它、listen 路径不调"的**隐性分叉**风险
#   （今天两者行为等价只是因为它是 no-op；一旦有人把它"修好"就会分叉）。
#   真正要修 HP 的高度元数据问题，需要一个**独立于 SOF** 的真值来源
#   （例如按熵编码数据实际覆盖的 MCU 行数反推），不是这里的比较逻辑能做的。


# ==================== 数据结构 ====================

@dataclass
class ScannerInfo:
    name: str
    ip: str
    port: int = 80
    escl_url: str = ""
    model: str = ""
    serial: str = ""
    vendor: str = "HP"
    has_adf: bool = False
    # ★ 2026-10-22：设备**是否明确报告过**输稿器能力。
    #   False = **没查到**（能力文档拿不到 / 文档里没提输稿器）⇒ **不等于没有**。
    #   实测 17.17.171.111 / .203 都是 M232 系列、**都有**输稿器，
    #   但一个的能力文档里没有 Adf 段、另一个的文档根本拿不到。
    adf_capability_known: bool = False
    has_duplex: bool = False
    has_platen: bool = True
    max_width: int = 2480   # A4 210mm @300dpi
    max_height: int = 3508  # A4 297mm @300dpi
    resolutions: list = field(default_factory=lambda: [75, 150, 200, 300, 600])
    color_modes: list = field(default_factory=lambda: ["RGB24", "Grayscale8"])
    formats: list = field(default_factory=lambda: ["image/jpeg", "application/pdf"])
    custom_name: str = ""
    # ★ 能力是否**真的拿到过**（2026-10-21 落地终局方案）
    # --------------------------------------------------------------
    # 由 `fetch_capabilities` 在「HTTP 200 + XML 可解析」时置 True；
    # 失败 / 超时 / 畸形 XML / 拿到的不是能力文档 ⇒ 保持 False。
    # 这是"设备到底有没有 ADF"的**唯一权威信号** —— 不再靠
    # "能力字段指纹前后有没有变"来猜（那种启发式在真实链路上有 3 条
    # 失效路径，其中 2 条是系统性的，见 scan_coordinator 里的说明）。
    capabilities_known: bool = False

    @property
    def display_name(self) -> str:
        return self.model or self.name


# ==================== 发现引擎 (OpenScanHub 思路) ====================

class _EsclListener(ServiceListener):
    def __init__(self):
        self.found: list[ScannerInfo] = []

    def add_service(self, zc: Zeroconf, service_type: str, name: str):
        try:
            info = zc.get_service_info(service_type, name)
            if not info or not info.addresses:
                return
            ip = info.parsed_addresses()[0]
            port = info.port or 80
            props = {}
            if info.properties:
                for k, v in info.properties.items():
                    key = k.decode() if isinstance(k, bytes) else str(k)
                    val = v.decode() if isinstance(v, bytes) else str(v)
                    props[key] = val

            scanner = ScannerInfo(
                name=info.name.rstrip(".") if info.name else f"Scanner-{ip}",
                ip=ip,
                port=port,
                model=props.get("ty", props.get("mdl", "")),
                serial=props.get("sern", props.get("usb_MFG", "")),
            )
            # 去重
            if not any(s.ip == scanner.ip for s in self.found):
                self.found.append(scanner)
        except Exception as e:
            logging.debug("mDNS 服务解析失败: %s", e)

    def remove_service(self, zc, type_, name):
        pass

    def update_service(self, zc, type_, name):
        pass


def discover_scanners(timeout: float = 4.0) -> list[ScannerInfo]:
    """mDNS 发现局域网 eSCL 扫描仪"""
    logger.info(f"mDNS 发现启动 (timeout={timeout}s)")
    zc = Zeroconf()
    listener = _EsclListener()

    # eSCL 通常注册在 _uscan._tcp.local.
    svc_types = ["_uscan._tcp.local.", "_scanner._tcp.local."]
    browsers = []
    for st in svc_types:
        try:
            browsers.append(ServiceBrowser(zc, st, listener))
            logger.info(f"  启动 ServiceBrowser: {st}")
        except Exception as e:
            logger.warning(f"  ServiceBrowser 失败 {st}: {e}")

    time.sleep(timeout)
    zc.close()
    logger.info(f"mDNS 发现完成: {len(listener.found)} 台")
    return listener.found


def probe_escl(ip: str, port: Optional[int] = None, timeout: float = 2.0,
               extra_ports=(), wake_retry: bool = True) -> Optional[str]:
    """
    探测指定 IP 的 eSCL 服务（休眠唤醒仅用于 ReadTimeout）。

    ★ `port` 语义（2026-10-21 修：此前该形参**被完全忽略**）
    ------------------------------------------------------------------
    改造前候选 base 恒为 `[http://{ip}:80, https://{ip}:443}]`，无论传什么
    端口，于是：
      - **非 80/443 端口的设备永远探不到**（形参形同虚设）；
      - 调用方"再试一次 HTTPS"（`port=443`）请求的 URL 集合与上一轮
        **完全相同** ⇒ 每台不可达设备多 4 次 HTTP 尝试 + 最多约 3 秒延迟。

    现在的规则：
      - `port` 不传（None，默认）⇒ 候选 `http://{ip}:80`、`https://{ip}:443`
        —— 与改造前**逐字一致**，保证既有调用方行为不变；
      - `port=80` ⇒ 同上（80 是标准 HTTP 端口，老候选已覆盖 `http:80`；
        GUI 端口框默认值就是 80，保持不变以免回归）；
      - `port=443` ⇒ 候选 `http://{ip}:443`、`https://{ip}:443`
        （与上面那组**不相同** ⇒ 不再是重复探测）；
      - 其它端口 P ⇒ 候选 `http://{ip}:P`、`https://{ip}:P`（非标端口设备可探）。

    ★ `extra_ports`（2026-10-21 新增）：**并入同一轮**候选表的额外端口。
      背景：CLI 原来分两轮探测 —— 第 1 轮 `probe_escl(ip)`（候选 http:80 +
      https:443），第 2 轮 `probe_escl(ip, port=443)`（候选 http:443 +
      https:443）。第 2 轮的 `https://{ip}:443` 与第 1 轮**整块重叠**
      ⇒ 同一个 URL 被重复请求（实测 404 形态 2 次、拒连/超时形态 1 次），
      每台不可达设备多 2~4 次 HTTP 尝试 + 最多约 10 秒延迟。
      现在改成：额外要覆盖的端口用 `extra_ports` 传进来，**一次性**并入
      候选表（已存在的 base 自动去重）⇒ 零重复，覆盖面不缩水。
      默认 `extra_ports=()` ⇒ 既有调用方行为**逐字不变**。
    """
    try:
        port_no = int(port) if port is not None else None
    except (TypeError, ValueError):
        port_no = None
    if not port_no or port_no == 80:
        # 标准端口：保持老候选（http:80 + https:443）
        bases = [f"http://{ip}:80", f"https://{ip}:443"]
        wake_port = 80
    else:
        bases = [f"http://{ip}:{port_no}", f"https://{ip}:{port_no}"]
        wake_port = port_no

    # 额外端口并入同一轮候选（去重 ⇒ 同一 base 不会被请求两次）
    for extra in (extra_ports or ()):
        try:
            extra_no = int(extra)
        except (TypeError, ValueError):
            continue
        if not extra_no:
            continue
        for scheme in ("http", "https"):
            base = f"{scheme}://{ip}:{extra_no}"
            if base not in bases:
                bases.append(base)

    paths = ("/eSCL/ScannerStatus", "/ScannerStatus")

    def _try():
        last_err = None
        for base in bases:
            for path in paths:
                try:
                    r = _http_call_bounded(
                        lambda b=base, p=path: requests.get(
                            f"{b}{p}", timeout=timeout, verify=False),
                        _wall_clock_for(timeout),
                        "探测 %s" % f"{base}{path}")
                    if r.status_code == 200:
                        return f"{base}/eSCL/" if "/eSCL" in path else f"{base}/", None
                except requests.ConnectionError:
                    break
                except requests.ReadTimeout as e:
                    last_err = e
                    break
                except Exception:
                    continue
        return None, last_err

    result, err = _try()
    if result:
        return result

    # 仅 ReadTimeout 时才做唤醒重试（打印机可能休眠）
    #
    # ★ 2026-10-22：`wake_retry=False` 可关掉它。
    #   背景：WSD 批量发现会对**内网所有** WSD 服务逐个探 eSCL，
    #   其中大部分不是扫描仪 ⇒ 不可达 ⇒ 走 ReadTimeout 分支 ⇒ 唤醒重试
    #   又要 +1.5 秒 sleep **再**跑一整轮候选 ⇒ 单 IP 最高 7.5 秒。
    #   （实测：内网 13 个候选 IP 串行 = 44 秒；13 个里只有 4 台是扫描仪。）
    #   ⇒ 批量探测时关掉唤醒重试：WSD 刚刚才收到这些设备的回应，
    #     它们并不需要被『唤醒』。
    if err and wake_retry:
        try:
            import socket
            s = socket.socket()
            s.settimeout(0.5)
            s.connect((ip, wake_port))
            s.close()
            time.sleep(1.5)
            r, _ = _try()
            if r:
                return r
        except Exception:
            pass
    return None


# ==================== 能力查询 (node-hp-scan-to 协议) ====================

def _probe_adf_from_status(scanner: ScannerInfo, base: str, timeout: float):
    """ScannerCapabilities 不可用时，从 ScannerStatus 回退检测 ADF"""
    try:
        r = _http_call_bounded(
            lambda: requests.get(f"{base}/ScannerStatus",
                                 timeout=(2.0, timeout), verify=False),
            _wall_clock_for((2.0, timeout)),
            "ScannerStatus 探测")
        if r.status_code == 200 and "AdfState" in r.text:
            # ★ 设备**明确报了** AdfState ⇒ 能力已知
            scanner.adf_capability_known = True
            scanner.has_adf = True
            logger.info("从 ScannerStatus 检测到 ADF: %s", scanner.ip)
    except Exception:
        pass


def _http_get_loose_tls(url: str, timeout: float):
    """
    用宽松 cipher 的独立连接请求 HTTPS。

    某些老打印机（实测 17.17.171.203 / .208）把 ScannerCapabilities 301 到
    HTTPS，而它们的 TLS 只支持老旧 cipher suite，OpenSSL 3.x 默认禁用导致
    握手失败（SSLV3_ALERT_HANDSHAKE_FAILURE）。

    仅用于**局域网内自签证书**设备兜底：check_hostname=False +
    verify_mode=CERT_NONE + 宽松 cipher 集合，且只作为 HTTP 路径已失败后的
    备用路径。返回 (status_code, text)；失败直接抛异常，由调用方兜住。
    """
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception:
        pass
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    except Exception:
        pass

    http = urllib3.PoolManager(ssl_context=ctx, cert_reqs="CERT_NONE")
    resp = http.request("GET", url, timeout=urllib3.Timeout(timeout))
    return resp.status, resp.data.decode("utf-8", errors="replace")


def fetch_capabilities(scanner: ScannerInfo, timeout: float = 3.0) -> ScannerInfo:
    """
    从 /eSCL/ScannerCapabilities 解析扫描仪全部能力。
    直接修改传入的 scanner 对象（原地更新）。

    ★ 能力是否"真的拿到"由 `scanner.capabilities_known` 表达（就地置位）：
      - **置 True**：HTTP 200 且 XML 可解析（拿到了能力文档）
      - **保持 False**：无 escl_url / 连接异常或超时 / 非 200（含 301→HTTPS
        兜底后仍非 200）/ XML 畸形
      - 已经 True 之后**不再清零**：能力一旦确认过就有效（后续一次查询失败
        不该把"已知"降级回"未知"，否则会重新制造"假未知"⇒ 丢失自动回退）。
    """
    if not scanner.escl_url:
        return scanner

    base = scanner.escl_url.rstrip("/")
    url = base + "/ScannerCapabilities"

    try:
        status = 0
        text = ""
        location = ""

        try:
            r = _http_call_bounded(
                lambda: requests.get(url, timeout=(2.0, timeout), verify=False,
                                     allow_redirects=False),
                _wall_clock_for((2.0, timeout)),
                "能力查询")
            status = r.status_code
            text = r.text
            location = r.headers.get("Location", "")
        except requests.RequestException as e:
            # https 直连握手失败（老机型 cipher 不兼容）→ 用宽松 TLS 重试
            logger.debug("能力查询直连失败，尝试宽松 TLS: %s", e)
            if url.lower().startswith("https://"):
                try:
                    status, text = _http_get_loose_tls(url, timeout)
                except Exception as e2:
                    logger.debug("宽松 TLS 重试失败: %s", e2)
            if status == 0:
                raise

        if status in (301, 302, 307, 308):
            # (a) 重定向目标是同协议的 http 路径 → 直接跟
            if location.lower().startswith("http://"):
                r2 = _http_call_bounded(
                    lambda: requests.get(location, timeout=(2.0, timeout),
                                         verify=False, allow_redirects=False),
                    _wall_clock_for((2.0, timeout)),
                    "能力查询（跟随重定向）")
                status = r2.status_code
                text = r2.text
                location = r2.headers.get("Location", "")
            # (b) 目标是 https，且这类老机型 TLS 只支持老旧 cipher
            if status in (301, 302, 307, 308) and location.lower().startswith("https://"):
                try:
                    status, text = _http_get_loose_tls(location, timeout)
                except Exception as e:
                    logger.debug("宽松 TLS 回退失败: %s", e)

        if status != 200:
            # ScannerCapabilities 失败 → 回退到 ScannerStatus 探测 ADF
            # （注意：这不是能力文档 ⇒ capabilities_known 保持 False）
            _probe_adf_from_status(scanner, base, timeout)
            return scanner
        root = ET.fromstring(text)

        # ★ 能力文档真的拿到了（HTTP 200 + XML 可解析）⇒ 置位"能力已知"。
        # 这是"设备到底有没有 ADF"的**唯一权威信号**：不再靠"字段有没有变"
        # 来猜（那种启发式在 WSD / GUI 链路上是幂等 no-op ⇒ 永远判"未知"）。
        scanner.capabilities_known = True
        # ★ 2026-10-22：能力文档拿到了，但**文档里没有 `<scan:Adf>` 段** ⇒
        #   只能判为「**输稿器能力未知**」（`adf_capability_known` 保持 False），
        #   **不能**判成「设备没有输稿器」—— 实测 17.17.171.111（M232dwc）
        #   明明有输稿器，文档里却没有这一段。

        # 型号 / 序列号
        # HP 真机报的是 pwg:MakeAndModel，部分机型报 Model，两个都认，
        # 同时存在时 MakeAndModel 优先（它是机型全称）
        model_tag = None
        for el in root.iter():
            t = _strip_ns(el)
            if t == "MakeAndModel" and el.text:
                model_tag = el.text.strip()
            elif t == "Model" and el.text and model_tag is None:
                model_tag = el.text.strip()
            if t == "SerialNumber" and el.text:
                scanner.serial = el.text.strip()
        if model_tag:
            scanner.model = model_tag

        # 平板 / 输稿器
        platen_sections = []
        adf_sections = []

        for child in root:
            t = _strip_ns(child)
            if t == "Platen":
                platen_sections.append(child)
            elif t == "Adf":
                adf_sections.append(child)
                scanner.has_adf = True
                # ★ 文档里**明确有** `<scan:Adf>` 段 ⇒ 能力已知
                scanner.adf_capability_known = True

        # 从 Platen 或 Adf 中提取能力
        source_section = None
        if platen_sections:
            source_section = platen_sections[0]
            scanner.has_platen = True
        if adf_sections:
            source_section = adf_sections[0]
            scanner.has_adf = True
            # 检测双面
            for el in adf_sections[0].iter():
                if _strip_ns(el) == "Duplex" and el.text and el.text.strip().lower() == "true":
                    scanner.has_duplex = True

        if source_section is None:
            source_section = root

        # 最大尺寸
        for el in source_section.iter():
            t = _strip_ns(el)
            if t == "MaxWidth":
                try:
                    scanner.max_width = int(el.text or "2550")
                except ValueError:
                    pass
            if t == "MaxHeight":
                try:
                    scanner.max_height = int(el.text or "4200")
                except ValueError:
                    pass

        # 分辨率
        resolutions = []
        for el in source_section.iter():
            t = _strip_ns(el)
            if t in ("DiscreteResolutions", "SupportedResolutions"):
                for child in el:
                    if _strip_ns(child) == "Width":
                        try:
                            dpi = int(child.text or "0")
                            if dpi > 0 and dpi not in resolutions:
                                resolutions.append(dpi)
                        except ValueError:
                            pass
        if resolutions:
            scanner.resolutions = sorted(resolutions)

        # 颜色模式
        color_modes = []
        for el in source_section.iter():
            t = _strip_ns(el)
            if t in ("ColorModes", "SupportedColorModes"):
                for child in el:
                    if child.text and child.text.strip():
                        color_modes.append(child.text.strip())
        if color_modes:
            scanner.color_modes = color_modes

        # 格式
        formats = []
        for el in source_section.iter():
            t = _strip_ns(el)
            if t in ("DocumentFormats", "SupportedDocumentFormats"):
                for child in el:
                    if child.text and child.text.strip():
                        formats.append(child.text.strip())
        if formats:
            scanner.formats = formats

    except requests.RequestException as e:
        logging.debug("能力查询失败 [%s]: %s", scanner.ip, e)
    except ET.ParseError as e:
        logging.debug("能力 XML 解析失败 [%s]: %s", scanner.ip, e)

    return scanner


def probe_scanner_status(escl_url: str, timeout: float = 5.0, session=None,
                         include_jobs: bool = False) -> tuple:
    """
    查询扫描仪状态，返回 (探测是否成功, 状态字典)。

    与 get_scanner_status 的区别：后者把连接失败、非 200、XML 畸形
    全部吞掉、返回 {"state": "Unknown", "adf_loaded": False}，调用方
    无法区分"探测失败"与"设备合法上报了 Unknown 状态"。协调器需要这个
    区分，否则设备上报 <State>Unknown</State> 时会被误判成探测失败，
    从而改变用户显式选择的来源。

    返回：
        (True,  {"state": "Idle", "adf_loaded": False})   探测成功
        (False, {"state": "Unknown", "adf_loaded": False}) 探测失败

    ★ session（可选）：复用调用方已有的会话（生产环境走建 job 的那个
      Session）⇒ 不新开连接、不重试握手；不传则沿用模块级 requests.get。
    ★ include_jobs（可选，默认 False）：额外解析 `scan:Jobs` / `scan:JobInfo`
      ⇒ 多出两个键 `pending_jobs`（未完成 job 数）与 `job_states`（其状态）。
      **默认 False** ⇒ 返回契约与既有调用方（cli.py / scan_coordinator.py）
      完全不变（键集合仍是 {"state", "adf_loaded"}）。
    """
    status = {"state": "Unknown", "adf_loaded": False,
              "adf_state": "", "job_states_all": [],
              "last_job_state": "", "has_completed_job": None}
    if include_jobs:
        status["pending_jobs"] = 0
        status["job_states"] = []
    try:
        url = escl_url.rstrip("/") + "/ScannerStatus"
        if session is not None:
            r = _http_call_bounded(
                lambda: session.get(url, timeout=timeout, verify=False),
                _wall_clock_for(timeout), "状态查询")
        else:
            r = _http_call_bounded(
                lambda: requests.get(url, timeout=timeout, verify=False),
                _wall_clock_for(timeout), "状态查询")
        if r.status_code != 200:
            return False, status
        root = ET.fromstring(r.text)
        for el in root.iter():
            t = _strip_ns(el)
            if t == "State" and el.text:
                status["state"] = el.text.strip()
            if t == "AdfState" and el.text:
                _adf = el.text.strip()
                status["adf_loaded"] = "Loaded" in _adf
                # ★ 2026-10-22：把 AdfState **原值**也带出来
                #   （公网实现 NAPS2 / sane-airscan 都细分到
                #    Empty / Jam / HatchOpen / Mispick …）
                #   ⇒ 我们才能区分「没纸」与「卡纸」，给出对症指引。
                status["adf_state"] = _adf
        all_jobs = _parse_job_states(root)
        status["job_states_all"] = ["%s" % s for _a, s in all_jobs]
        status["has_completed_job"] = (
            any(s.lower() == "completed" for _a, s in all_jobs)
            if all_jobs else None)
        if all_jobs:
            status["last_job_state"] = all_jobs[0][1]
        if include_jobs:
            job_states = _parse_pending_job_states(root)
            status["pending_jobs"] = len(job_states)
            status["job_states"] = job_states
        return True, status
    except (requests.RequestException, ET.ParseError) as e:
        logger.debug("状态查询失败: %s", e)
        return False, status


# ScanJob 的**终态**（这些状态说明这个 job 已经结束了，不再挡着新建 job）。
_TERMINAL_JOB_STATES = {"completed", "canceled", "cancelled", "error",
                         "failed", "aborted"}


def _job_is_unfinished(job_state: str, images_to_transfer: int) -> bool:
    """
    这个 ScanJob 是否**还没结束**（= 还挡着新建 job）。

    判据（真机实测形态）：
      - 状态非终态（Pending / Processing / Scanning / Transferring…）⇒ 未完成；
      - 状态是终态但 `JobImagesToTransfer > 0` ⇒ 设备还有图没交出去 ⇒ 未完成
        （真机第一轮实测到的残留 job 形态：ImagesToTransfer=1）；
      - `Completed` ⇒ 那个 job 已经做完了（顶多是张没取走的旧图，**不挡路**）
        ⇒ 不算未完成；
      - `Canceled` 且没有待传图像 ⇒ 设备只是在收尾 ⇒ 不算未完成
        （真机第二轮：DELETE 之后设备回 Idle，2.27 秒后建 job 就成功）。
    """
    st = (job_state or "").strip().lower()
    if st and st not in _TERMINAL_JOB_STATES:
        return True
    if st == "completed":
        return False
    return images_to_transfer > 0


# 设备侧「**没正常收尾**」的作业状态（终态，但不是 Completed）。
# ★ 实测依据（2026-10-22，四台真机逐台验）：
#   · 有一条 Completed 的设备 ⇒ POST /ScanJobs **201** ✔
#   · 最后一条是 Canceled/Aborted 的设备 ⇒ POST **409**（响应体全空，
#     而 pwg:State 却报 Idle ⇒ 用户只看到「打印机坏了」）；
#   · DELETE 那几条残留 ⇒ 404/410 ⇒ **客户端清不掉**；
#   · 正常收尾的设备扫完再 POST 仍然 201 ✔。
# ⇒ 判据是「**有没有正常收尾的作业**」，不是「有没有旧记录」
#   （`.101` 也有旧记录，照样能扫）。
_ABNORMAL_JOB_STATES = {"canceled", "cancelled", "aborted",
                          "error", "failed"}


def _parse_job_states(root) -> list:
    """设备侧全部作业记录，按**新旧**排序（新的在前）。

    每条 = (age_seconds, state)。`age` 是设备自报的作业年龄（单位秒）。
    ★ 实测：`.203` 的 `age` 是 4290491337 秒（≈136 年）⇒ **设备端计数器异常**，
    所以排序要**容错**：age 明显异常（> 10^8）的记录排在最后，不让它盖掉
    正常的记录。
    """
    out = []
    for el in root.iter():
        if _strip_ns(el) != "JobInfo":
            continue
        state = ""
        age = 0
        for child in el.iter():
            t = _strip_ns(child)
            if t == "JobState" and child.text:
                state = child.text.strip()
            elif t == "Age" and child.text:
                try:
                    age = int(float(child.text.strip()))
                except (TypeError, ValueError):
                    age = 0
        if state:
            out.append((age, state))
    # 新的在前；age 异常的（> 10^8 秒 ≈ 3 年）排到最后
    # 新的在前（age 越大越新）；age 明显异常（> 10^8 秒 ≈ 3 年）的排到最后，
    # 不让它盖掉正常记录（实测 `.203` 的 age 是 4290491337 秒 ≈ 136 年）。
    out.sort(key=lambda x: (x[0] > 10 ** 8, -x[0]))
    return out


def _parse_pending_job_states(root) -> list:
    """
    从已解析的 ScannerStatus 文档里挑出**未完成**的 ScanJob 状态列表。

    真机实测形态：`scan:Jobs` / `scan:JobInfo` 里挂着那个还没结束的 job，
    `JobImagesToTransfer` 还 > 0（设备还有图没交出去）。
    """
    pending = []
    for el in root.iter():
        if _strip_ns(el) != "JobInfo":
            continue
        state = ""
        images = 0
        for child in el.iter():
            t = _strip_ns(child)
            if t == "JobState" and child.text:
                state = child.text.strip()
            elif t == "JobImagesToTransfer" and child.text:
                try:
                    images = int(str(child.text).strip())
                except (TypeError, ValueError):
                    images = 0
        if _job_is_unfinished(state, images):
            pending.append(state or "Unknown")
    return pending


def get_scanner_status(escl_url: str, timeout: float = 5.0) -> dict:
    """查询扫描仪状态（保留原有契约：失败时返回 Unknown 状态字典）"""
    ok, status = probe_scanner_status(escl_url, timeout)
    return status


# ==================== 扫描执行 ====================

NS_SCAN = "http://schemas.hp.com/imaging/escl/2011/05/03"
NS_PWG = "http://www.pwg.org/schemas/2010/12/sm"


def _build_scan_job_xml(
    resolution: int = 300,
    color_mode: str = "RGB24",
    doc_format: str = "image/jpeg",
    width: int = 2550,
    height: int = 4200,
    source: str = "Platen",
    duplex: bool = False,
    max_pages: int = 1,
) -> str:
    """构建 POST /eSCL/ScanJobs 请求体，严格参照 node-hp-scan-to 抓包格式"""
    # ADF 扫描需要设置 MaxScanPages 为较大值以支持多页
    max_pages_xml = f"<scan:MaxScanPages>{max_pages}</scan:MaxScanPages>" if source == "Feeder" else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{NS_SCAN}" xmlns:pwg="{NS_PWG}">
  <pwg:Version>2.0</pwg:Version>
  <pwg:DocumentFormat>{doc_format}</pwg:DocumentFormat>
  <pwg:InputSource>{source}</pwg:InputSource>
  <scan:Intent>Document</scan:Intent>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:Width>{width}</pwg:Width>
      <pwg:Height>{height}</pwg:Height>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <scan:XResolution>{resolution}</scan:XResolution>
  <scan:YResolution>{resolution}</scan:YResolution>
  <scan:ColorMode>{color_mode}</scan:ColorMode>
  <scan:Duplex>{str(duplex).lower()}</scan:Duplex>
  {max_pages_xml}
</scan:ScanSettings>"""


# 格式映射：用户友好的名称 → MIME
FORMAT_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",
    "tiff": "image/tiff",
    "pdf": "application/pdf",
}
MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
    "image/bmp": "bmp",
    "application/pdf": "pdf",
    "application/octet-stream": "jpg",
}

# 用户选择的输出格式 → (向打印机请求的 MIME, 落盘扩展名)
#
# 打印机**永远只出图片**，PDF 由本地合成（save_manager）。这样"选 PDF"与
# "选 JPG"走同一条代码路径，落盘层与请求层对 PDF 的理解不再冲突。
# FORMAT_MIME / MIME_EXT 保留不动（可能有其它调用方引用），扫描请求才用这里。
REQUEST_FORMAT = {
    "jpg":  ("image/jpeg", "jpg"),
    "jpeg": ("image/jpeg", "jpg"),
    "png":  ("image/png",  "png"),
    "tiff": ("image/tiff", "tiff"),
    "tif":  ("image/tiff", "tiff"),
    "pdf":  ("image/jpeg", "pdf"),   # 请求图片，落盘时本地合成 PDF
}

# 409（打印机上残留未完成任务）的有界退避：2/4/8 秒，共 14 秒。
#
# ★ 2026-10-22 实测修订：**删掉了"TTL 15-20 分钟"这个不实结论**。
#   早先这里写"设备侧残留 job 的 TTL 实测 15-20 分钟"，文案据此让用户
#   "等 15-20 分钟"。**那个结论是错的**，实测证据：
#     ① 设备回收"被遗弃的 job"（建了但没取走数据、也没删掉）的 TTL ≈ **90 秒**
#        （实测 81~94 秒）⇒ 远不到 15 分钟；
#     ② 但设备上**残留的 Canceled / Aborted 记录**（幽灵 job）实测**跨月不回收**：
#        17.17.171.203 的两条记录 **30 天后仍在**（2026-10-22 只读复验：
#        仍是 Canceled ×2、State=Idle），.208 的 26 小时、.111 的 2.6 小时；
#        且这些记录客户端 DELETE 返回 404/410 ⇒ **客户端清不掉**。
#   ⇒ 「等 N 分钟就会好」是**假承诺**：等 15 分钟可能白等，等 30 天也可能没好。
#   ⇒ 文案只能给**确定有效**的下一步：去打印机面板取消 / 重启打印机。
#   ⇒ 有界退避（2/4/8 秒共 14 秒）依旧正确：既然等不出来，就别傻等。
_CONFLICT_BACKOFF_SECONDS = (2.0, 4.0, 8.0)

# 409 的错误文案：必须写清"为什么"和"用户能做什么"，**且不得给假的时间承诺**。
#
# ★ 2026-10-22 一并修掉两处不实表述：
#   ① "设备回收需要 15-20 分钟" ⇒ 不实（见上）⇒ 改成"不会很快自行消失"；
#   ② "该型号不支持客户端清理" ⇒ **措辞偏宽**：真机实测该型号**支持逐个删**
#      （DELETE 具体 job URI ⇒ HTTP 200；只是 DELETE 集合路径
#      /eSCL/ScanJobs ⇒ 405），真正的原因是**这些残留记录早已 404/410**，
#      删了也没用 ⇒ 改成"客户端也清不掉（设备返回 404/410）"。
_CONFLICT_TTL_HINT = "不会很快自行消失"
_CONFLICT_MESSAGE = (
    "打印机上残留了一条上次未完成的扫描任务记录（HTTP 409），重试 3 次仍未释放。"
    "这类残留记录不会很快自行消失，客户端也清不掉（设备返回 404/410）："
    "请到打印机面板上查看并取消该任务；若面板上找不到该任务，"
    "请把打印机**断电 10 秒后重新通电**再试（面板上的「重启」键可能清不掉）。"
)

# ★ 2026-10-22 新增：**先探设备状态**再报的 409 文案。
#   实测依据（四台真机逐台验，2026-10-22）：
#     · 设备侧最后一条作业是 Canceled/Aborted ⇒ POST /ScanJobs 返 409，
#       **响应体全空**，而 pwg:State 仍报 Idle ⇒ 用户只看到「打印机坏了」；
#     · 有一条 Completed 的设备（.101）⇒ POST **201** ✔；
#     · DELETE 那几条残留 ⇒ **404/410** ⇒ 客户端清不掉。
#   ⇒ 所以要说清：**是「上次没扫完」留下的**，不是打印机坏了；
#     并给出**立刻能做的动作**。
_CONFLICT_MESSAGE_STALE = (
    "设备上**最近一次**扫描没有正常结束（作业状态：{state}），"
    "所以它现在不再受理新的扫描任务。"
    "请在打印机面板上按「取消 / 确认」键结束那个任务；"
    "若面板上找不到它，请把打印机**断电 10 秒后重新通电**"
    "（面板上的「重启」键可能清不掉这个标记）。"
)

# ★ 2026-10-23：设备侧**一条作业记录都没有**，却仍然 409 ⇒
#   说明设备内部还留着「上次任务没正常结束」的标记，而**状态接口看不到它**。
#   （实测 17.17.171.208 / .111：记录已空，仍 409。）
_CONFLICT_MESSAGE_NO_TRACE = (
    "打印机内部还留着上次任务没有正常结束的标记"
    "（状态接口里查不到任何作业记录），所以它现在不再受理新的扫描任务。"
    "请在打印机面板上按「取消 / 确认」键；"
    "若面板上没有反应，请把打印机**断电 10 秒后重新通电**再试。"
)


def _conflict_message_for(base: str, session=None) -> str:
    """409 的文案：**先探一次设备状态** ⇒ 给出准确的因果与恢复动作。

    ★ 判据与三种情形（实测 2026-10-23，四台真机 + 重启前后对照）：
      · 设备侧**有一条 Completed**（成功扫完过）⇒ 它其实能扫（如 17.17.171.101），
        409 多半是别的偶发因素 ⇒ 走通用文案；
      · 有记录但**没有 Completed**（最近一次是 Canceled/Aborted）⇒ 设备侧确实留着
        「上次没正常结束」的记录 ⇒ 说清是哪一次、什么状态；
      · **一条记录都没有** ⇒ 设备内部还留着标记、状态接口看不到（实测 .208/.111
        重启后记录已空却仍 409）⇒ **必须断电**，面板重启可能清不掉。
    """
    try:
        ok, st = probe_scanner_status(base, timeout=3.0, session=session)
    except Exception:
        ok, st = False, {}
    if ok:
        last = st.get("last_job_state") or ""
        has_completed = st.get("has_completed_job")
        if last and has_completed is False:
            return _CONFLICT_MESSAGE_STALE.format(state=last)
        if has_completed is None:
            # 设备侧**一条记录都没有**（has_completed_job=None）
            return _CONFLICT_MESSAGE_NO_TRACE
    return _CONFLICT_MESSAGE

# 503 的有界退避**按来源分流**（2026-10-21 主控拍板）。
#
# ★ 判据（两种 503 的成因不同，处置也必须不同）：
#   ① Platen（平板）/ 来源未知 —— 503 来自"设备还没从上一个 ScanJob 收尾干净"。
#      真机复验（2026-10-21，DELETE 掉上一个 job 之后紧接着建 job）：
#      **2.27 秒就成功**，压根没收到 503 ⇒ 瞬时收尾只要 ~2.3 秒。
#      ⇒ 退避 2+4 秒（6 秒）足够覆盖这个窗口；再长就是白等。
#   ② Feeder（输稿器）—— 503 来自"输稿器空载、设备在等纸"。
#      实测 2026-10-20（D1 基准）：输稿器空载时，创建任务阶段就返回 503。
#      ⇒ **等多久都不会成功**（除非用户中途放纸，而 UI 不会提示他去放）⇒
#      退避是**白等** ⇒ **不重试**，拿到 503 立刻抛可执行错误。
#
# ★ 2026-10-21 改：档位由 2+4+8（14 秒）降为 **2+4（6 秒）**。
#   依据：① 瞬时收尾实测只需 2.27 秒 ⇒ 14 秒偏长；
#         ② 真正难缠的"设备上残留未完成 job"场景**等多久都不会自愈**
#            （残留记录实测 30 天都不回收）⇒ 靠**退避**解决不了，靠的是
#            **退避用尽后查设备状态 + 给出"去面板取消"的正确指引**
#            （见 `_probe_pending_job` 与下面的 pending 版文案）。
_SERVICE_UNAVAILABLE_BACKOFF_SECONDS = (2.0, 4.0)        # 平板 / 来源未知
_SERVICE_UNAVAILABLE_BACKOFF_SECONDS_FEEDER = ()          # 输稿器空载：不重试

# 503 的错误文案：**必须按扫描来源给不同的下一步指引**。
#
# ★ 2026-10-21 修（旧文案只有输稿器那一句，是错的）：
#   旧文案 = "打印机当前不可用（HTTP 503）。若选择的是输稿器(ADF)扫描，
#   请确认输稿器里已放入纸张后重试；否则请等待设备空闲后重试。"
#   但本次请求 XML 里的 `InputSource` 决定 503 的真实原因：
#     - Platen（平板）⇒ 设备还没从上一个 job 收尾 ⇒ "去输稿器放纸"是
#       **指错方向**，用户会去白跑一趟输稿器；
#     - Feeder（输稿器）⇒ 输稿器空载 ⇒ "确认已放纸"才是对症的话。
#   所以文案按 source 分流（见 `_service_unavailable_message`）。
#
# 输稿器那一条**不含**"已自动重试 N 次"—— 输稿器路径**根本没有重试**
# （见上面的分流判据），提了就是假话。
_SERVICE_UNAVAILABLE_MESSAGE_PLEN = (
    "打印机当前不可用（HTTP 503，来源：平板）：设备尚未就绪，"
    "可能仍在收尾上一个扫描任务。请稍候重试"
    "（已自动重试 {retries} 次仍未成功）；"
    "若持续失败，请确认打印机面板上没有其它任务在执行。"
)
_SERVICE_UNAVAILABLE_MESSAGE_FEEDER = (
    "打印机当前不可用（HTTP 503，来源：输稿器）：输稿器里没有纸，"
    "设备正在等纸（不是设备故障）。"
    "请往输稿器里放入纸张，放纸后请重试。"
)

# 503 的错误文案：**设备上确有未完成 ScanJob** 时的版本
# （2026-10-21 真机复验后新增）。
#
# ★ 为什么必须分这一版（真机两轮实测）：
#   503 的主要成因是"**设备上存在一个未完成的 ScanJob**"（残留的、或正在
#   处理的），**不是**"设备还在收尾上一个 job"：
#     - 第一轮：探针把上一个 job 留在设备上没删 ⇒ 设备状态 Processing、
#       ScannerStatus 里该 job 仍在、ImagesToTransfer=1 ⇒ 建新 job 持续 503，
#       **退避 14 秒也没恢复**（残留记录实测 30 天都不回收）；
#     - 第二轮：DELETE 正确执行后（设备回 Idle）⇒ 紧接着建 job 2.27 秒就成功。
#   ⇒ 对"残留 job"这个**主要**场景，"请稍候重试"是**无效指引**（干等没用），
#     用户真正该做的是"**到打印机面板上取消那个任务**"。
#   ⇒ 反之，若设备上**没有**未完成 job（真的只是瞬时收尾）⇒ 保留"请稍候重试"。
#
# 平板那条仍然**不得**出现「输稿器」（会把用户指到错误的方向）。
#
# ★ pending 版**不含**"已自动重试 N 次"：查到未完成 job 时是**立刻**抛错的
#   （压根没退避过），写上重试次数就是假话。
_SERVICE_UNAVAILABLE_MESSAGE_PLEN_PENDING = (
    "打印机当前不可用（HTTP 503，来源：平板）：打印机上有一个未完成的"
    "扫描任务，请到打印机面板上取消该任务（或等它结束后再重试）。"
    "已停止自动等待——该任务不会很快自行结束。"
)
# 输稿器 + 残留 job ⇒ **两件事都要说**：先取消任务，纸也还是要放
#（输稿器空载确实是 503 的成因之一 ⇒ "放纸"指引不能丢）。
_SERVICE_UNAVAILABLE_MESSAGE_FEEDER_PENDING = (
    "打印机当前不可用（HTTP 503，来源：输稿器）：打印机上有一个未完成的"
    "扫描任务，请到打印机面板上取消该任务（或等它结束后再重试）；"
    "输稿器扫描还需确认已放入纸张（放纸后请重试）。"
)

# 传输层失败（连不上 / 超时）的用户文案。
# ★ 为什么要专门翻译：requests 抛的是 urllib3 的英文长串
#   （`HTTPConnectionPool(host='127.0.0.1', port=1): Max retries exceeded
#   with url: /eSCL/ScanJobs (Caused by NewConnectionError(...))`），
#   GUI 把它原样弹给用户 ⇒ 用户看到一堆英文，看不到"下一步该做什么"，
#   与 `ScanDeviceError` 的设计意图（消息里已写清用户下一步）相悖。
_CONNECT_ERROR_MESSAGE = (
    "无法连接打印机，请确认已开机并与本机在同一网络；"
    "若刚刚断网或打印机刚从休眠唤醒，请稍后重试。"
)
_TIMEOUT_ERROR_MESSAGE = (
    "连接打印机超时，请确认已开机并与本机在同一网络；"
    "若打印机刚从休眠唤醒，请稍后重试。"
)

# 超时文案：必须写清"设备没响应"这件事本身 + 用户能做的下一步。
# 单独提成常量（而不是就地写在 raise 里）—— CLI 的错误呈现测试要断言
# 的就是**生产环境真正抛出的这段文字**。
SCAN_TIMEOUT_MESSAGE = (
    "扫描超时：未能在限定时间内获取扫描数据。"
    "请确认打印机已开机并完成初始化、面板上没有其它任务在执行；"
    "若设备刚从休眠唤醒，请稍后重试。"
)
FIRST_PAGE_TIMEOUT_MESSAGE = (
    "扫描超时：未能获取第一页数据。"
    "请确认打印机已开机并完成初始化、面板上没有其它任务在执行；"
    "若设备刚从休眠唤醒，请稍后重试。"
)


class ScanCancelled(RuntimeError):
    """扫描被用户取消 —— eSCL / 监听 / WIA 三条引擎路径**统一**的取消信号。

    协调器靠识别"取消"与"失败"的区别来决定发 `scan_cancelled` 还是
    `scan_failed`：取消必须抛本异常（或其子类），不能当成普通失败。
    """


class ScanJobCancelled(ScanCancelled):
    """创建扫描任务期间被用户取消（409 退避等待过程中收到取消信号）。"""


class ScanDeviceError(RuntimeError):
    """设备 / 协议类**可操作**错误：消息里已写清用户下一步该做什么。

    与"意外错误"（代码 bug、依赖缺失、网络环境异常等）区分开：
    CLI 遇到本异常时把消息**原样**呈现给用户即可，不必再甩 traceback；
    意外错误才保留 traceback 便于排障。
    """


def _sleep_cancellable(seconds, cancel_event=None, slice_seconds=0.2):
    """
    可取消的等待。返回 True = 等待期间被取消。

    - 有 cancel_event：分片睡眠（每片 0.2 秒），每片检查一次取消信号，
      用户点取消后最多再等 0.2 秒就生效（原先一次 sleep 14 秒，用户无法中断）
    - 无 cancel_event：一次 time.sleep(seconds)（保持既有调用形态不变）
    """
    if cancel_event is None:
        time.sleep(seconds)
        return False
    remaining = float(seconds)
    while remaining > 0:
        if cancel_event.is_set():
            return True
        step = min(slice_seconds, remaining)
        time.sleep(step)
        remaining -= step
    return bool(cancel_event.is_set())


def _service_unavailable_backoff(source: str) -> tuple:
    """
    503 的退避档位**按来源分流**（判据见 `_SERVICE_UNAVAILABLE_BACKOFF_SECONDS`
    上方的注释）：

    - Feeder（输稿器）⇒ **空 tuple = 不重试**：503 来自"输稿器空载、等纸"，
      等多久都不会成功 ⇒ 立刻报错，别让用户白等；
    - Platen（平板）/ 来源未知 ⇒ (2, 4) 秒（共 6 秒）：503 来自"设备还没从上一个
      ScanJob 收尾干净"，实测 **2.27 秒**就自愈 ⇒ 6 秒足够覆盖。
    """
    if (source or "").strip().lower() == "feeder":
        return _SERVICE_UNAVAILABLE_BACKOFF_SECONDS_FEEDER
    return _SERVICE_UNAVAILABLE_BACKOFF_SECONDS


# 查设备状态的**超时上限**：状态查询只是失败路径上的诊断动作，**不得**变成
# 阻塞式的长时间等待 ⇒ 硬上限 3 秒。
_STATUS_PROBE_TIMEOUT = 3.0


def _probe_pending_job(session, escl_url: str,
                       timeout: float = _STATUS_PROBE_TIMEOUT):
    """
    查设备上有没有**未完成**的 ScanJob（复用既有的 ScannerStatus 探测）。

    返回 True / False / None：
      True  —— 设备上确有未完成的 ScanJob（用户该去打印机面板取消它）；
      False —— 查到了，但设备上没有未完成 job（真的只是瞬时收尾）；
      None  —— **查不到**（查询失败 / 超时 / XML 畸形 / 非 200）。

    ★ 容错铁律：这里的**任何**异常都在内部吞掉 ⇒ 绝不让"查状态失败"变成
      "用户看到崩溃"（调用方对 None 与 False 的处理是一样的：都退回旧文案）。
    ★ 只在**已经收到 503** 之后才被调用（第一次 503 时探一次，整条失败路径
      只探这一次）⇒ 正常路径零开销。
    ★ 探测**不回答**"要不要重试"（那是来源分流的职责），只回答
      "等下去有没有意义" ⇒ True 时调用方应**立即报错**（继续等必然失败）。
    """
    try:
        ok, status = probe_scanner_status(
            escl_url, timeout=timeout, session=session, include_jobs=True)
    except Exception as e:            # 兜底：任何意外都不得冒泡给用户
        logger.debug("设备状态查询异常（忽略）: %s", e)
        return None
    if not ok:
        return None
    try:
        pending = int(status.get("pending_jobs") or 0)
    except (TypeError, ValueError):
        return None
    if pending > 0:
        logger.info("设备上有未完成的 ScanJob（%s 个）", pending)
    return pending > 0


def _service_unavailable_message(source: str, retries: int = 0,
                                 pending_job: bool = False) -> str:
    """
    按扫描来源给出**不同**的 503 指引。

    - Feeder（输稿器）⇒ 设备在等纸 ⇒ 保留"放纸后请重试"（**不重试**，
      所以文案里不提"已自动重试"）；
    - 其它（Platen 平板 / 未知来源）⇒ 设备多半还没从上一个 job 收尾干净
      ⇒ 指引是"设备尚未就绪，请稍候重试"，**不得**把用户指向输稿器。

    ★ pending_job=True（设备上确有未完成的 ScanJob）："请稍候重试"是
      **无效指引**（真机实测：退避 14 秒也没恢复，残留记录 30 天都不回收）⇒ 换成
      "到打印机面板上取消该任务"。来源差异仍然保留（输稿器那条仍带放纸指引）。
    ★ pending 版**不提**"已自动重试 N 次"：探测到未完成 job 时是**立刻**抛的，
      压根没退避过。
    """
    is_feeder = (source or "").strip().lower() == "feeder"
    if pending_job:
        if is_feeder:
            return _SERVICE_UNAVAILABLE_MESSAGE_FEEDER_PENDING
        return _SERVICE_UNAVAILABLE_MESSAGE_PLEN_PENDING
    if is_feeder:
        return _SERVICE_UNAVAILABLE_MESSAGE_FEEDER
    return _SERVICE_UNAVAILABLE_MESSAGE_PLEN.format(retries=retries)


def _transport_error_to_device_error(exc: BaseException) -> ScanDeviceError:
    """
    把 requests 的传输层异常翻译成**可操作的中文**设备错误。

    ★ 根因不许丢：原始异常用 `raise ... from exc` 链在 `__cause__` 上
      （协调器 `logger.error(..., exc_info=True)` 会把整条链打进日志，
      CLI 的 traceback 路径也拿得到）；消息里只留**异常类型名**做面包屑，
      不把 urllib3 的英文长串甩给用户。
    """
    if isinstance(exc, requests.Timeout):
        base = _TIMEOUT_ERROR_MESSAGE
    else:
        base = _CONNECT_ERROR_MESSAGE
    return ScanDeviceError(f"{base}（原始错误类型：{type(exc).__name__}）")



# ══════════════════════════════════════════════════════════════════
# 「设备侧没删掉的作业」清单（2026-10-22 新增，治本）
# ══════════════════════════════════════════════════════════════════
# ★ 背景（实测 + 公网证据）：
#   · NAPS2 `EsclScanDriver.cs:213-217`：「Some devices (e.g. Kyocera) **stay busy
#     until the completed job is deleted** … so we always send DELETE for the job」；
#     NAPS2 issue #906：「删掉已完成的 job 后设备才解锁」。
#   · 我们实测：设备在处理中时 `DELETE` **长时间不返回**（6 秒读超时）
#     ⇒ 旧实现 3 秒就放弃 ⇒ 作业停在「没收尾」状态 ⇒ 设备侧记 Canceled/Aborted
#     ⇒ **那台打印机从此 POST 409**，客户端再 DELETE 只会 404/410（清不掉）
#     ⇒ 只能重启打印机。
#     ⇒ 2026-10-22 实测 `.208` 的两条 Canceled Age=2.0 天、`.111` 的 1.0 天
#       —— **就是这条路把我自己的三台打印机搞成扫不了的。**
# ⇒ 做法：DELETE 失败/超时就**记进清单**（持久化，跨进程），
#   **下一次建 job 之前**先把清单里的作业逐个补删。
_PENDING_JOBS_FILE = "pending_jobs.json"
_PENDING_JOBS_MAX = 50


def _pending_jobs_path() -> Optional[str]:
    r"""清单文件位置：%APPDATA%\HP_Scan_Tool\pending_jobs.json（与其它配置同目录）"""
    appdata = os.environ.get("APPDATA", "")
    base = appdata if appdata else os.path.expanduser("~")
    d = os.path.join(base, "HP_Scan_Tool")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return os.path.join(d, _PENDING_JOBS_FILE)


def load_pending_jobs() -> list:
    """读出「设备侧可能还没删掉的作业」清单（读不到就当空）"""
    p = _pending_jobs_path()
    if not p or not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [str(x) for x in data if isinstance(x, str)]
    except Exception:
        return []


def save_pending_jobs(uris) -> None:
    p = _pending_jobs_path()
    if not p:
        return
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(sorted(set(uris))[:_PENDING_JOBS_MAX], fh,
                      ensure_ascii=False, indent=2)
    except Exception:
        pass


def remember_pending_job(uri: str) -> None:
    """记下一个「可能还没被设备删掉」的作业 URI"""
    if not uri:
        return
    try:
        cur = load_pending_jobs()
        if uri not in cur:
            cur.append(uri)
            save_pending_jobs(cur)
    except Exception:
        pass


def forget_pending_job(uri: str) -> None:
    try:
        cur = load_pending_jobs()
        if uri in cur:
            cur.remove(uri)
            save_pending_jobs(cur)
    except Exception:
        pass


def _abs_job_uri(base: str, uri: str) -> str:
    """把设备给的作业 URI 变成绝对 URL。

    ★ 设备报的 `pwg:JobUri` 形如 `/eSCL/ScanJobs/<uuid>` —— 那是**相对主机根**的
      路径，而 base 形如 `http://host:80/eSCL/` ⇒ 必须取 base 的 **origin** 再拼，
      否则会拼成 `http://host:80/eSCL/eSCL/ScanJobs/...`（错地址 ⇒ DELETE 404 ⇒
      被误当成「设备已回收」而从清单里丢掉 ⇒ 恰恰丢掉了最该补删的那条）。
    """
    if uri.startswith("http"):
        return uri
    root = base.rstrip("/")
    if "/eSCL" in root:
        root = root.split("/eSCL")[0]
    if not uri.startswith("/"):
        uri = "/" + uri
    return root + uri
    root = base.rstrip("/")
    if not root.endswith("/eSCL"):
        root += "/eSCL"
    return root + uri


def cleanup_pending_jobs(session=None, base: str = "", timeout: float = 8.0) -> int:
    """把清单里的作业逐个 DELETE 掉；删成功的从清单移除。

    ★ 在**建新 job 之前**调用 ⇒ 设备不会因为「上次那个作业没删」而拒接新任务。
    ★ 删不掉的（404/410 = 设备已回收）也**从清单移除** —— 那种记录设备自己会清，
      留着只是噪音。
    返回：本次成功删掉的个数。
    """
    uris = load_pending_jobs()
    if not uris:
        return 0
    remain = []
    done = 0
    for uri in uris:
        url = _abs_job_uri(base, uri) if base else uri
        ok = False
        try:
            if session is not None:
                r = session.delete(url, timeout=timeout)
                ok = r.status_code in (200, 201, 202, 204, 404, 410)
            else:
                r = requests.delete(url, timeout=timeout, verify=False)
                ok = r.status_code in (200, 201, 202, 204, 404, 410)
        except Exception:
            ok = False
        if ok:
            done += 1
        else:
            remain.append(uri)
    save_pending_jobs(remain)
    if done:
        logger.info("清理设备侧残留作业 %d 条（剩余 %d 条待清）", done, len(remain))
    return done



def _create_scan_job(session, scanner, resolution, color_mode, output_format, source, duplex,
                      max_pages=1, cancel_event=None):
    """
    创建扫描任务（POST ScanJobs），返回 (base, job_uri, ext)。
    调用者负责关闭 session。

    source：本次请求的 InputSource（Platen / Feeder）—— 503 的错误文案
    **按它分流**（平板的 503 不得把用户指向输稿器）。

    cancel_event：可选的取消信号，409 / 503 退避等待期间用它及时中断。
    """
    escl_url = scanner.escl_url
    if not escl_url:
        raise ScanDeviceError("未配置 eSCL URL")

    base = escl_url.rstrip("/")
    fmt = (output_format or "jpg").lower()
    if fmt not in REQUEST_FORMAT:
        # ★ 不静默降级：不支持的输出格式必须显式报错。
        #   旧实现直接回退 ("image/jpeg", "jpg")，用户要的 bmp 无声消失，
        #   产出的是 .jpg 而用户以为拿到了 .bmp（历史缺陷 M-28）。
        raise ScanDeviceError(
            f"不支持的输出格式: {output_format}（支持: "
            f"{'、'.join(sorted(REQUEST_FORMAT))}）")
    mime, ext = REQUEST_FORMAT[fmt]

    xml_body = _build_scan_job_xml(
        resolution=resolution,
        color_mode=color_mode,
        doc_format=mime,
        width=scanner.max_width,
        height=scanner.max_height,
        source=source,
        duplex=duplex,
        max_pages=max_pages,
    )

    headers = {"Content-Type": "application/xml"}
    # ★ 2026-10-22：**先补删**上次没删掉的作业 ⇒ 设备不会因为
    #   「上次那个作业没收尾」而拒接新任务（POST 409）。
    try:
        cleanup_pending_jobs(session, base)
    except Exception:
        pass

    job_url = f"{base}/ScanJobs"

    def _post():
        """POST ScanJobs。传输层异常 → 中文可操作错误（根因链在 __cause__）。"""
        try:
            return _http_call_bounded(
                lambda: session.post(job_url, data=xml_body, headers=headers,
                                     timeout=30),
                _wall_clock_for(30), "创建扫描任务")
        except requests.RequestException as e:
            raise _transport_error_to_device_error(e) from e

    # ★ 409（设备残留 job）与 503 都是**可重试**的瞬时状态 ⇒ 共用同一套有界
    #   退避写法：有限次数 + 有限退避 + 等待可被取消。
    #   差异在两处：① 409 的退避档位固定；② **503 的退避档位按来源分流**
    #   （平板会自愈 → 重试；输稿器空载 → 等不出来 → 不重试）。
    r = _post()
    conflict_retry = 0
    unavailable_retry = 0
    unavailable_backoff = _service_unavailable_backoff(source)
    # 503 的设备状态**整条失败路径只探一次**（在**第一次** 503 时探）。
    probed_pending = False
    pending_job = False
    while True:
        if r.status_code == 409:
            # 打印机上残留了一条未完成的 ScanJob 记录（此前某次扫描异常中断
            # 留下的），设备因此拒绝再受理新任务（409，响应体全空、State 报 Idle）。
            # ★ 2026-10-22 实测修订（**不再承诺任何"等多久会好"**）：
            #   - 该型号**支持逐个删** job（DELETE 具体 job URI ⇒ 200），
            #     只是 DELETE 集合路径 /eSCL/ScanJobs ⇒ 405；
            #   - 但这些残留记录早已被设备回收（DELETE ⇒ 404/410）⇒ 客户端清不掉；
            #   - 且残留记录实测**跨月不回收**（.203 的两条 30 天后仍在）
            #     ⇒ 等 15 分钟 / 等半小时都可能白等。
            # ⇒ 退避是**有界**的（2/4/8 秒共 14 秒）：等不出来就别傻等，
            #   立刻报错并给出确定有效的下一步（面板取消 / 重启打印机）。
            # 等待过程可被 cancel_event 随时中断（原先一次 sleep 14 秒不可取消）。
            if conflict_retry >= len(_CONFLICT_BACKOFF_SECONDS):
                raise ScanDeviceError(_conflict_message_for(base, session))
            delay = _CONFLICT_BACKOFF_SECONDS[conflict_retry]
            conflict_retry += 1
            logger.info("扫描任务被占用 (409)，%s 秒后重试: %s", delay, scanner.ip)
            if _sleep_cancellable(delay, cancel_event):
                raise ScanJobCancelled("扫描已取消（等待打印机释放任务时中断）")
            r = _post()
            continue
        if r.status_code == 503:
            # 实测 2026-10-20：source=Feeder 且输稿器为空时，打印机在创建任务
            # 阶段就返回 503（设备在等纸），不是扫描超时。
            # 实测 2026-10-21（真机两轮复验）：503 的**主要**成因是"设备上
            # 存在一个未完成的 ScanJob"（残留的 / 正在处理的）：
            #   - 探针把上一个 job 留在设备上没删 ⇒ 设备状态 Processing、
            #     ScannerStatus 里该 job 仍在、ImagesToTransfer=1 ⇒ 建新 job
            #     持续 503，**退避 14 秒也没恢复**（残留记录 30 天都不回收）；
            #   - DELETE 正确执行后（设备回 Idle）⇒ 紧接着建 job 2.27 秒就成功。
            # ⇒ 退避档位**按来源分流**（判据见 `_service_unavailable_backoff`）：
            #   平板/未知 → (2,4) 秒重试（瞬时收尾实测 2.27 秒自愈，6 秒够）；
            #   输稿器   → 不重试（等不到，立刻告诉用户去放纸）。
            #
            # ★ 2026-10-21 主控拍板：**探测前移到第一次 503**（不是退避用尽后）。
            #   时序：
            #     第一次 503 ⇒ 先探一次设备状态（≤3 秒、容错、只探这一次）
            #       ├─ 探到**未完成 job** ⇒ 建新 job **必然失败**（真机实测：
            #       │   退避 14 秒也没恢复，残留记录 30 天都不回收）⇒ 继续退避是**白等**
            #       │   ⇒ **立即抛错**，文案指向"到打印机面板上取消该任务"
            #       │   （平板与输稿器一视同仁；输稿器那条仍保留放纸指引）。
            #       └─ 探测失败/查不到 ⇒ 按来源走原有退避（平板 2+4=6 秒；
            #           输稿器不退避）⇒ 退避用尽后再抛原来的错。
            #   ⇒ 「残留 job」场景从"等 6 秒还失败"变成"立刻告诉用户该做什么"；
            #     「瞬时收尾」场景仍是 6 秒内自动恢复 —— 两类场景都不吃亏。
            if not probed_pending:
                probed_pending = True
                # 用户已取消 ⇒ 不必再花一次 HTTP 去查状态（取消优先）。
                if cancel_event is not None and cancel_event.is_set():
                    raise ScanJobCancelled("扫描已取消（等待打印机就绪时中断）")
                # ★ 只在**已经收到 503** 的失败路径上才探状态（正常路径零开销）；
                #   查询失败/超时/畸形一律被吞掉（见 `_probe_pending_job`）
                #   ⇒ 绝不让"查状态失败"变成用户看到的崩溃。
                pending_job = _probe_pending_job(session, base)
                if pending_job:
                    raise ScanDeviceError(
                        _service_unavailable_message(source, unavailable_retry,
                                                     True))
            if unavailable_retry >= len(unavailable_backoff):
                # 走到这里说明**没有**探测到未完成 job（探测到就已立刻抛了）
                raise ScanDeviceError(
                    _service_unavailable_message(source, unavailable_retry,
                                                 pending_job))
            delay = unavailable_backoff[unavailable_retry]
            unavailable_retry += 1
            logger.info("打印机暂不可用 (503)，%s 秒后重试: %s", delay, scanner.ip)
            if _sleep_cancellable(delay, cancel_event):
                raise ScanJobCancelled("扫描已取消（等待打印机就绪时中断）")
            r = _post()
            continue
        break

    if r.status_code not in (200, 201, 202):
        raise ScanDeviceError(f"创建扫描任务失败 HTTP {r.status_code}: {r.text[:300]}")

    job_uri = r.headers.get("Location", "")
    if not job_uri:
        try:
            root = ET.fromstring(r.text)
            for el in root.iter():
                if "JobUri" in (el.tag.split("}")[-1] if "}" in el.tag else el.tag):
                    job_uri = el.text or ""
                    break
        except Exception:
            pass

    if not job_uri:
        raise ScanDeviceError("无法获取 ScanJob URI")

    if not job_uri.startswith("http"):
        host = base.split("://")[1].split("/")[0]
        job_uri = f"http://{host}{job_uri}"

    return base, job_uri, ext


def execute_scan(
    scanner: ScannerInfo,
    resolution: int = 300,
    color_mode: str = "RGB24",
    output_format: str = "jpg",
    source: str = "Platen",
    duplex: bool = False,
    timeout: float = 30.0,
    cancel_event=None,
) -> tuple[bytes, str]:
    """
    执行一次完整扫描，返回 (数据, 实际扩展名)。
    流程: POST ScanJobs → 轮询 JobState → GET NextDocument
    """
    session = requests.Session()
    session.verify = False
    job_uri = None
    ext = REQUEST_FORMAT.get((output_format or "jpg").lower(), ("image/jpeg", "jpg"))[1]

    try:
        # ADF 扫描需要设置 MaxScanPages
        max_pages = 1000 if source == "Feeder" else 1
        try:
            base, job_uri, ext = _create_scan_job(
                session, scanner, resolution, color_mode, output_format, source, duplex,
                max_pages=max_pages, cancel_event=cancel_event)
        except ScanJobCancelled:
            # 409 退避期间被用户取消：按"取消"语义返回（协调器识别为取消）
            return None, ext

        # 轮询 NextDocument（更短的间隔 + 更长的读取超时捕捉扫描数据）
        start = time.time()
        nd_url = job_uri.rstrip("/") + "/NextDocument"
        while time.time() - start < timeout:
            if cancel_event and cancel_event.is_set():
                return None, ext

            try:
                r = _http_call_bounded(
                    lambda: session.get(nd_url, timeout=15),
                    _wall_clock_for(15), "取扫描数据")
                if r.status_code == 200:
                    return r.content, ext
                elif r.status_code == 503:
                    pass  # 扫描进行中
                elif r.status_code == 410:
                    # 扫描已完成但数据已过期（打印机已返回过200但客户端没取到）
                    raise ScanDeviceError("扫描数据已过期（HTTP 410），请放纸后重试")
            except requests.RequestException:
                pass

            # 短间隔快速轮询
            for _ in range(3):
                if cancel_event and cancel_event.is_set():
                    return None, ext
                time.sleep(0.1)

        raise ScanDeviceError(SCAN_TIMEOUT_MESSAGE)
    finally:
        # 清理打印机上的扫描任务（防止任务累积导致409冲突）
        if job_uri:
            # ★ 2026-10-22：DELETE 超时放宽到 8 秒并**重试一次**。
            #   实测：设备在处理中时 DELETE 会**长时间不返回**（我们 6 秒读超时）
            #   ⇒ 原来 3 秒就放弃 ⇒ 作业停在「没收尾」状态 ⇒ 设备侧留下
            #   Canceled/Aborted 记录 ⇒ **那台打印机从此拒接新作业（409）**，
            #   而客户端再去 DELETE 它只会得到 404/410（清不掉）⇒ 只能重启打印机。
            #   ⇒ 这几行就是「我自己把机器搞成扫不了」的源头，必须收紧。
            for _attempt in range(2):
                try:
                    session.delete(job_uri, timeout=8)
                    break
                except Exception:
                    continue
            else:
                # ★ 两次都没删掉 ⇒ 设备可能还留着它 ⇒ 记进清单，
                #   **下次建 job 之前**先补删（见 cleanup_pending_jobs）。
                remember_pending_job(job_uri)
        session.close()


# ── 「设备一个文档都没给」的错误文案与判定（2026-10-22 新增）────────────
# ★ 为什么要有（用户实测原话：「扫描会自动取消」）：
#   旧实现把「取页时第一个 NextDocument 就回 404」当成「没有更多页了」**立刻
#   return**（哪怕 0 页）⇒ 返回空列表 ⇒ 协调器按**取消**处理 ⇒ 界面显示
#   「扫描已取消」，而真实原因（输稿器没纸 / 设备里没东西可扫）
#   **一个字都没告诉用户**。
#
# ★ eSCL 规范语义（经 node-hp-scan-to 的抓包文档**逐字引用** Mopria eSCL 规范）：
#     404 = "no more page. The last page has already been transmitted."
#     410 = "the scan job doesn't exist anymore."
#     503 = "the job is active, but the scanner can't return the payload at
#            the moment. Retry later."
#   ⇒「还没准备好」是 **503** 的语义；404/410 一页都没有 ⇒ 作业确实没产出文档。
#   ⇒ 公网实现（NAPS2 / sane-airscan）都有独立的 NO_DOCS / FeederEmpty 状态，
#     **没有一个**把「0 页」报成「取消」。
_NO_DOCUMENT_MESSAGE_FEEDER = (
    "设备没有返回任何扫描数据：输稿器（ADF）里没有纸，或纸没有被设备吸入。"
    "请确认输稿器里已放入纸张后重试。"
)
_NO_DOCUMENT_MESSAGE_PLEN = (
    "设备没有返回任何扫描数据：平板上没有可扫描的文档。"
    "请把要扫描的纸张放到平板玻璃上后重试。"
)
_NO_DOCUMENT_MESSAGE_JAM = (
    "设备没有返回任何扫描数据：输稿器（ADF）状态异常（卡纸 / 托盘故障 / 盖板未关）。"
    "请打开打印机面板处理卡纸、关好盖板、放好纸张后重试。"
)

# 「空 404」时的设备状态复查节流（秒）—— 免得对着设备狂刷状态查询
_EMPTY_STATUS_PROBE_INTERVAL = 2.0


def _no_document_message(source: str, adf_state: str = "") -> str:
    """按来源给「设备没给出任何数据」的文案（输稿器 / 平板各一套）"""
    _st = (adf_state or "").strip().lower()
    if "jam" in _st or "hatchopen" in _st or "tray" in _st:
        return _NO_DOCUMENT_MESSAGE_JAM
    if (source or "").strip().lower() == "feeder":
        return _NO_DOCUMENT_MESSAGE_FEEDER
    return _NO_DOCUMENT_MESSAGE_PLEN


def _device_still_working(session, base: str) -> bool:
    """设备还在处理这个 job 吗？（区分「还没开始吐数据」与「做完了但没给东西」）

    ★ 依据 ScannerStatus 的 State 与未完成 job 数（`include_jobs=True`）。
    ★ 探测失败 ⇒ **保守当成"还在忙"**（宁可多等，也不要把"设备慢"误报成失败）。
    ★ 公网实现（NAPS2 / node-hp-scan-to）判定"作业真结束"靠的正是
      ScannerStatus 里的 JobState / JobStateReason ⇒ 我们把这条接上了。
    """
    try:
        ok, st = probe_scanner_status(base, timeout=3.0, session=session,
                                      include_jobs=True)
    except Exception:
        return True
    if not ok:
        return True
    try:
        if int(st.get("pending_jobs") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    state = str(st.get("state") or "").strip().lower()
    return state in ("processing", "scanning", "transferring", "pending")



def execute_multipage_scan(
    scanner: ScannerInfo,
    resolution: int = 300,
    color_mode: str = "RGB24",
    output_format: str = "jpg",
    source: str = "Platen",
    duplex: bool = False,
    timeout: float = 90.0,
    page_timeout: float = 30.0,
    progress_callback=None,
    cancel_event=None,
) -> list[tuple[bytes, str]]:
    """
    执行多页 ADF 扫描，返回 [(数据, 扩展名), ...]。
    循环获取 NextDocument 直到 HTTP 404（无更多页面）。
    progress_callback(page_num, total_so_far) 可选，用于 UI 进度更新。
    """
    session = requests.Session()
    session.verify = False
    job_uri = None
    ext = REQUEST_FORMAT.get((output_format or "jpg").lower(), ("image/jpeg", "jpg"))[1]

    try:
        # ADF 扫描需要设置 MaxScanPages 为较大值
        max_pages = 1000 if source == "Feeder" else 1
        try:
            base, job_uri, ext = _create_scan_job(
                session, scanner, resolution, color_mode, output_format, source, duplex,
                max_pages=max_pages, cancel_event=cancel_event)
        except ScanJobCancelled:
            return [], ext

        # 循环获取页面
        pages = []
        nd_url = job_uri.rstrip("/") + "/NextDocument"
        overall_start = time.time()
        # ★ 2026-10-22：把「**已经拿到过页**」与「**一页都没拿到**」分开处理。
        #   旧实现：404 ⇒ 立刻 return pages（**哪怕 0 页**）⇒ 空列表
        #          ⇒ 协调器按**取消**处理 ⇒ 界面「扫描已取消」，无任何指引。
        #   新实现：
        #     · 已经拿到过页之后的 404/410 ⇒ 正常收尾（行为不变）；
        #     · **一页都没拿到**的 404/410 ⇒ 作业没产出任何文档：
        #         用 ScannerStatus 交叉验证一次（设备还忙？还在处理就继续等；
        #         闲下来了就确定没产出）⇒ 抛**带指引的错误**，不再冒充"取消"。
        empty_document_seen = False
        last_status_probe = 0.0

        while time.time() - overall_start < timeout:
            # 检查取消
            if cancel_event and cancel_event.is_set():
                return pages if pages else []

            # 等待当前页面就绪
            page_start = time.time()
            got_page = False
            while time.time() - page_start < page_timeout:
                if cancel_event and cancel_event.is_set():
                    return pages if pages else []

                try:
                    r = _http_call_bounded(
                    lambda: session.get(nd_url, timeout=15),
                    _wall_clock_for(15), "取扫描数据")
                    if r.status_code == 200:
                        pages.append((r.content, ext))
                        if progress_callback:
                            progress_callback(len(pages), len(pages))
                        time.sleep(0.3)
                        got_page = True
                        break
                    elif r.status_code in (404, 410):
                        if pages:
                            # 已经拿到过页 ⇒ 这是「没有更多页了」的正常收尾信号
                            return pages
                        # ★ 一页都没有 ⇒ 作业没产出任何文档。
                        #   规范里「还没准备好」是 503 的语义，404/410 出现即作业
                        #   已结束；但为防个别机型过早回 404，用 ScannerStatus
                        #   交叉验证：设备还忙 ⇒ 继续等；闲下来了 ⇒ 确定没产出。
                        empty_document_seen = True
                        _now = time.time()
                        if _now - last_status_probe >= _EMPTY_STATUS_PROBE_INTERVAL:
                            last_status_probe = _now
                            if not _device_still_working(session, base):
                                _adf_state = ""
                                try:
                                    _ok, _st = probe_scanner_status(
                                        base, timeout=3.0, session=session)
                                    _adf_state = (_st or {}).get("adf_state", "")
                                except Exception:
                                    pass
                                raise ScanDeviceError(
                                    _no_document_message(source, _adf_state))
                    elif r.status_code == 503:
                        pass
                except requests.RequestException:
                    pass

                for _ in range(3):
                    if cancel_event and cancel_event.is_set():
                        return pages if pages else []
                    time.sleep(0.1)

            if got_page:
                continue
            # ── 页级窗口用完，一页都没拿到 ──
            if pages:
                break  # 已有页面，结束扫描
            if empty_document_seen and not _device_still_working(session, base):
                raise ScanDeviceError(_no_document_message(source))
            # 设备还在忙 ⇒ 继续等（外层还有总预算 timeout）

        # ── 总预算用尽 ──
        if not pages:
            # ★ 一页都没拿到、又不是用户取消 ⇒ **绝不能**返回空列表
            #   （空列表会被协调器当成"取消"⇒ 界面显示「扫描已取消」）
            raise ScanDeviceError(_no_document_message(source))
        return pages
    finally:
        if job_uri:
            # ★ 2026-10-22：DELETE 超时放宽到 8 秒并**重试一次**。
            #   实测：设备在处理中时 DELETE 会**长时间不返回**（我们 6 秒读超时）
            #   ⇒ 原来 3 秒就放弃 ⇒ 作业停在「没收尾」状态 ⇒ 设备侧留下
            #   Canceled/Aborted 记录 ⇒ **那台打印机从此拒接新作业（409）**，
            #   而客户端再去 DELETE 它只会得到 404/410（清不掉）⇒ 只能重启打印机。
            #   ⇒ 这几行就是「我自己把机器搞成扫不了」的源头，必须收紧。
            for _attempt in range(2):
                try:
                    session.delete(job_uri, timeout=8)
                    break
                except Exception:
                    continue
            else:
                # ★ 两次都没删掉 ⇒ 设备可能还留着它 ⇒ 记进清单，
                #   **下次建 job 之前**先补删（见 cleanup_pending_jobs）。
                remember_pending_job(job_uri)
        session.close()


def auto_crop(img: Image.Image, threshold: int = 240, padding: int = 5) -> Image.Image:
    """
    自动裁边：检测并裁剪图像周围的空白/黑边区域。

    基于像素亮度方差检测边缘，适用于 ADF 扫描产生的黑边、
    边缘杂色等常见场景。

    Args:
        img: 输入图像
        threshold: 亮度阈值 (0-255)，高于此值视为空白
        padding: 裁剪后保留的边距像素

    Returns:
        裁剪后的图像（无需裁剪则返回原图）
    """
    # 转为灰度图进行边缘检测
    gray = img.convert("L")
    pixels = gray.load()
    width, height = gray.size

    # 从四个方向检测边界
    def is_row_blank(y):
        """判断某行是否为空白"""
        for x in range(width):
            if pixels[x, y] < threshold:
                return False
        return True

    def is_col_blank(x):
        """判断某列是否为空白"""
        for y in range(height):
            if pixels[x, y] < threshold:
                return False
        return True

    # 从上往下找第一行非空白
    top = 0
    while top < height and is_row_blank(top):
        top += 1

    # 从下往上找第一行非空白
    bottom = height - 1
    while bottom >= top and is_row_blank(bottom):
        bottom -= 1

    # 从左往右找第一列非空白
    left = 0
    while left < width and is_col_blank(left):
        left += 1

    # 从右往左找第一列非空白
    right = width - 1
    while right >= left and is_col_blank(right):
        right -= 1

    # 如果没有需要裁剪的边，返回原图
    if top == 0 and bottom == height - 1 and left == 0 and right == width - 1:
        return img

    # 添加边距
    top = max(0, top - padding)
    bottom = min(height - 1, bottom + padding)
    left = max(0, left - padding)
    right = min(width - 1, right + padding)

    return img.crop((left, top, right + 1, bottom + 1))


def crop_to_a4(img: Image.Image, dpi: int = 300) -> Image.Image:
    """居中裁剪到 A4 (210x297mm)，消除扫描仪过扫白边"""
    w, h = img.size
    a4_w = min(int(210 / 25.4 * dpi), w)  # 不超过原图
    a4_h = min(int(297 / 25.4 * dpi), h)
    left = max(0, (w - a4_w) // 2)
    top = max(0, (h - a4_h) // 2)
    return img.crop((left, top, left + a4_w, top + a4_h))


def is_blank_page(img: Image.Image, threshold: float = 0.02) -> bool:
    """
    检测是否为空白页。

    基于像素方差检测：将图像转为灰度后计算像素值的标准差，
    标准差低于阈值视为空白页（页面内容极少）。

    Args:
        img: 输入图像
        threshold: 方差阈值 (0-1)，低于此值视为空白，默认 0.02

    Returns:
        True 如果是空白页，False 否则
    """
    # 转为灰度图并缩小以加速计算
    gray = img.convert("L")
    # 缩小到 100x100 以加速
    small = gray.resize((100, 100), Image.LANCZOS)
    # 计算像素方差
    pixels = list(small.getdata())
    mean = sum(pixels) / len(pixels)
    variance = sum((p - mean) ** 2 for p in pixels) / len(pixels)
    # 归一化方差 (0-1)
    normalized_variance = variance / (255.0 ** 2)
    return normalized_variance < threshold
