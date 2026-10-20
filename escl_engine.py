"""
eSCL (AirScan) 扫描引擎 — 合并版
参考：
  - OpenScanHub: 多协议架构、ADF 检测
  - node-hp-scan-to: 完整 eSCL 协议交互流程(ScannerCapabilities → ScannerStatus → ScanJobs → NextDocument)
  - photo-scan-split: 极简 mDNS 发现
"""

import copy
import io
import logging
import struct
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
    "execute_scan", "execute_multipage_scan", "scan_to_file",
    "auto_crop", "is_blank_page", "FORMAT_MIME", "MIME_EXT",
    "REQUEST_FORMAT", "ScanJobCancelled",
]


def _strip_ns(el) -> str:
    """剥离 XML 元素的 namespace 前缀，返回本地标签名。"""
    t = el.tag if hasattr(el, "tag") else str(el)
    return t.split("}")[-1] if "}" in t else t


def _fix_jpeg_header(data: bytes) -> bytes:
    """
    修复 HP 扫描仪 JPEG 头部高度元数据错误。

    部分 HP 机型在 JPEG SOF 段中报告的高度值与实际图像数据不匹配，
    导致图像底部被裁剪或显示异常。此函数检测并修复该问题。

    参考: node-hp-scan-to 的 JpegUtil 实现。
    """
    # 检查是否为 JPEG（SOI 标记 FF D8）
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return data

    # 从实际图像数据获取真实高度
    try:
        img = Image.open(io.BytesIO(data))
        actual_height = img.height
    except Exception:
        return data

    # 解析 JPEG 标记，查找 SOF0/SOF2 (Start of Frame)
    pos = 2  # 跳过 SOI
    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        # SOF0 (基线 DCT) 或 SOF2 (渐进 DCT)
        if marker in (0xC0, 0xC2):
            # SOF 段结构: FF C0 [长度2] [精度1] [高度2] [宽度2] ...
            if pos + 9 < len(data):
                header_height = struct.unpack(">H", data[pos + 5:pos + 7])[0]
                if header_height != actual_height:
                    # 修复高度值
                    data = bytearray(data)
                    data[pos + 5:pos + 7] = struct.pack(">H", actual_height)
                    data = bytes(data)
                    logging.debug(
                        "JPEG 头部修复: 高度 %d → %d", header_height, actual_height
                    )
            break
        # 跳过当前标记段
        if pos + 3 < len(data):
            seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            pos += 2 + seg_len
        else:
            break

    return data


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
    has_duplex: bool = False
    has_platen: bool = True
    max_width: int = 2480   # A4 210mm @300dpi
    max_height: int = 3508  # A4 297mm @300dpi
    resolutions: list = field(default_factory=lambda: [75, 150, 200, 300, 600])
    color_modes: list = field(default_factory=lambda: ["RGB24", "Grayscale8"])
    formats: list = field(default_factory=lambda: ["image/jpeg", "application/pdf"])
    custom_name: str = ""

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


def probe_escl(ip: str, port: int = 80, timeout: float = 2.0) -> Optional[str]:
    """探测指定 IP 的 eSCL 服务（休眠唤醒仅用于 ReadTimeout）"""
    bases = [f"http://{ip}:80", f"https://{ip}:443"]
    paths = ("/eSCL/ScannerStatus", "/ScannerStatus")

    def _try():
        last_err = None
        for base in bases:
            for path in paths:
                try:
                    r = requests.get(f"{base}{path}", timeout=timeout, verify=False)
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
    if err:
        try:
            import socket
            s = socket.socket()
            s.settimeout(0.5)
            s.connect((ip, 80))
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
        r = requests.get(f"{base}/ScannerStatus", timeout=(2.0, timeout), verify=False)
        if r.status_code == 200 and "AdfState" in r.text:
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
            r = requests.get(url, timeout=(2.0, timeout), verify=False,
                             allow_redirects=False)
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
                r2 = requests.get(location, timeout=(2.0, timeout), verify=False,
                                  allow_redirects=False)
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
            _probe_adf_from_status(scanner, base, timeout)
            return scanner
        root = ET.fromstring(text)

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


def probe_scanner_status(escl_url: str, timeout: float = 5.0) -> tuple:
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
    """
    status = {"state": "Unknown", "adf_loaded": False}
    try:
        r = requests.get(escl_url.rstrip("/") + "/ScannerStatus",
                         timeout=timeout, verify=False)
        if r.status_code != 200:
            return False, status
        root = ET.fromstring(r.text)
        for el in root.iter():
            t = _strip_ns(el)
            if t == "State" and el.text:
                status["state"] = el.text.strip()
            if t == "AdfState" and el.text:
                status["adf_loaded"] = "Loaded" in (el.text.strip() or "")
        return True, status
    except (requests.RequestException, ET.ParseError) as e:
        logger.debug("状态查询失败: %s", e)
        return False, status


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
# 设备侧残留 job 的 TTL 实测 15-20 分钟 ⇒ 等满 14 秒也不可能等到回收，
# 所以这里只做**有界**退避（期间可被用户取消），随后立即报错并明确告知 TTL。
_CONFLICT_BACKOFF_SECONDS = (2.0, 4.0, 8.0)

# 409 的错误文案：必须写清"设备回收要多久"和"用户能做什么"
_CONFLICT_TTL_HINT = "15-20 分钟"
_CONFLICT_MESSAGE = (
    "打印机上残留了一个未完成的扫描任务（HTTP 409），重试 3 次仍未释放。"
    "该型号不支持客户端清理，设备回收需要 15-20 分钟："
    "请在打印机面板上取消该任务，或稍后重试；若持续失败可重启打印机。"
)


class ScanJobCancelled(RuntimeError):
    """创建扫描任务期间被用户取消（409 退避等待过程中收到取消信号）。"""


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


def _create_scan_job(session, scanner, resolution, color_mode, output_format, source, duplex,
                      max_pages=1, cancel_event=None):
    """
    创建扫描任务（POST ScanJobs），返回 (base, job_uri, ext)。
    调用者负责关闭 session。

    cancel_event：可选的取消信号，409 退避等待期间用它及时中断。
    """
    escl_url = scanner.escl_url
    if not escl_url:
        raise RuntimeError("未配置 eSCL URL")

    base = escl_url.rstrip("/")
    fmt = (output_format or "jpg").lower()
    if fmt not in REQUEST_FORMAT:
        # ★ 不静默降级：不支持的输出格式必须显式报错。
        #   旧实现直接回退 ("image/jpeg", "jpg")，用户要的 bmp 无声消失，
        #   产出的是 .jpg 而用户以为拿到了 .bmp（历史缺陷 M-28）。
        raise RuntimeError(
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
    r = session.post(f"{base}/ScanJobs", data=xml_body, headers=headers, timeout=30)
    if r.status_code == 409:
        # 打印机上残留一个未完成的 ScanJob（此前某次扫描异常中断留下的）。
        # 该型号不支持列举与整体删除（GET/DELETE /eSCL/ScanJobs 均返回 405），
        # 客户端无法主动清理，只能等打印机侧 TTL 回收（实测 15-20 分钟）。
        #
        # 退避是**有界**的（默认 2/4/8 秒共 14 秒）：等满 14 秒也等不到
        # 15-20 分钟的 TTL，与其傻等，不如立刻报错并告诉用户要等多久。
        # 等待过程可被 cancel_event 随时中断（原先一次 sleep 14 秒不可取消）。
        for delay in _CONFLICT_BACKOFF_SECONDS:
            logger.info("扫描任务被占用 (409)，%s 秒后重试: %s", delay, scanner.ip)
            if _sleep_cancellable(delay, cancel_event):
                raise ScanJobCancelled("扫描已取消（等待打印机释放任务时中断）")
            r = session.post(f"{base}/ScanJobs", data=xml_body, headers=headers, timeout=30)
            if r.status_code != 409:
                break
        else:
            raise RuntimeError(_CONFLICT_MESSAGE)
    if r.status_code == 503:
        # 实测 2026-10-20：source=Feeder 且输稿器为空时，打印机在创建任务
        # 阶段就返回 503（设备在等纸），不是扫描超时。给出用户可执行动作。
        raise RuntimeError(
            "打印机当前不可用（HTTP 503）。"
            "若选择的是输稿器(ADF)扫描，请确认输稿器里已放入纸张后重试；"
            "否则请等待设备空闲后重试。"
        )
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"创建扫描任务失败 HTTP {r.status_code}: {r.text[:300]}")

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
        raise RuntimeError("无法获取 ScanJob URI")

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
                r = session.get(nd_url, timeout=15)
                if r.status_code == 200:
                    return _fix_jpeg_header(r.content), ext
                elif r.status_code == 503:
                    pass  # 扫描进行中
                elif r.status_code == 410:
                    # 扫描已完成但数据已过期（打印机已返回过200但客户端没取到）
                    raise RuntimeError("扫描数据已过期（HTTP 410），请放纸后重试")
            except requests.RequestException:
                pass

            # 短间隔快速轮询
            for _ in range(3):
                if cancel_event and cancel_event.is_set():
                    return None, ext
                time.sleep(0.1)

        raise RuntimeError("扫描超时：未能在限定时间内获取扫描数据")
    finally:
        # 清理打印机上的扫描任务（防止任务累积导致409冲突）
        if job_uri:
            try:
                session.delete(job_uri, timeout=3)
            except Exception:
                pass
        session.close()


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

        while time.time() - overall_start < timeout:
            # 检查取消
            if cancel_event and cancel_event.is_set():
                return pages if pages else []

            # 等待当前页面就绪
            page_start = time.time()
            while time.time() - page_start < page_timeout:
                if cancel_event and cancel_event.is_set():
                    return pages if pages else []

                try:
                    r = session.get(nd_url, timeout=15)
                    if r.status_code == 200:
                        pages.append((_fix_jpeg_header(r.content), ext))
                        if progress_callback:
                            progress_callback(len(pages), len(pages))
                        time.sleep(0.3)
                        break
                    elif r.status_code == 404:
                        return pages
                    elif r.status_code == 410:
                        return pages  # 扫描过期，当前页面不可用
                    elif r.status_code == 503:
                        pass
                except requests.RequestException:
                    pass

                for _ in range(3):
                    if cancel_event and cancel_event.is_set():
                        return pages if pages else []
                    time.sleep(0.1)
            else:
                # 单页超时
                if pages:
                    break  # 已有页面，结束扫描
                raise RuntimeError("扫描超时：未能获取第一页数据")

        return pages
    finally:
        if job_uri:
            try:
                session.delete(job_uri, timeout=3)
            except Exception:
                pass
        session.close()


def scan_to_file(
    scanner: ScannerInfo,
    output_path: str,
    resolution: int = 300,
    color_mode: str = "RGB24",
    output_format: str = "jpg",
    source: str = "Platen",
    duplex: bool = False,
    timeout: float = 120.0,
) -> str:
    """扫描并保存到文件，返回文件路径。"""
    data, ext = execute_scan(
        scanner=scanner,
        resolution=resolution,
        color_mode=color_mode,
        output_format=output_format,
        source=source,
        duplex=duplex,
        timeout=timeout,
    )

    # PDF：打印机只出图片（见 REQUEST_FORMAT），PDF 在本地合成。
    # 若设备确实直出了 PDF 字节（老设备/直连调用方），原样落盘。
    if output_format.lower() == "pdf":
        pdf_path = output_path.rsplit(".", 1)[0] + ".pdf"
        if data[:5] == b"%PDF-":
            with open(pdf_path, "wb") as f:
                f.write(data)
        else:
            with Image.open(io.BytesIO(data)) as src:
                src.convert("RGB").save(pdf_path, "PDF")
        return pdf_path

    # 确保扩展名正确
    final_path = output_path
    if not output_path.lower().endswith(f".{ext}") and not output_path.lower().endswith(f".{output_format}"):
        final_path = f"{output_path}.{ext}"

    with open(final_path, "wb") as f:
        f.write(data)

    return final_path


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
