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
    "fetch_capabilities", "get_scanner_status",
    "execute_scan", "execute_multipage_scan", "scan_to_file",
    "auto_crop", "is_blank_page", "FORMAT_MIME", "MIME_EXT",
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
    max_width: int = 2550
    max_height: int = 4200
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
    zc = Zeroconf()
    listener = _EsclListener()

    # eSCL 通常注册在 _uscan._tcp.local.
    svc_types = ["_uscan._tcp.local.", "_scanner._tcp.local."]
    browsers = []
    for st in svc_types:
        try:
            browsers.append(ServiceBrowser(zc, st, listener))
        except Exception:
            pass

    time.sleep(timeout)
    zc.close()
    return listener.found


def probe_escl(ip: str, port: int = 80, timeout: float = 4.0) -> Optional[str]:
    """探测指定 IP 的 eSCL 服务，返回完整 eSCL 基础 URL"""
    # HP 打印机常见端口：HTTP 80, HTTPS 443, 备用 8080/8443/53048
    probe_configs = [
        (f"http://{ip}:80",      "HTTP:80"),
        (f"https://{ip}:443",    "HTTPS:443"),
        (f"https://{ip}:53048",  "HTTPS:53048"),
        (f"http://{ip}:8080",    "HTTP:8080"),
        (f"https://{ip}:8443",   "HTTPS:8443"),
    ]

    paths = ("/eSCL/ScannerStatus", "/ScannerStatus")

    for base, label in probe_configs:
        for path in paths:
            try:
                r = requests.get(f"{base}{path}", timeout=timeout, verify=False)
                if r.status_code == 200:
                    logger.info("probe_escl %s 成功: %s%s", ip, base, path)
                    if "/eSCL" in path:
                        return f"{base}/eSCL/"
                    return f"{base}/"
            except requests.ConnectionError:
                break  # 端口不通，不用再试该 base 的其他 path
            except Exception:
                continue

    logger.warning("probe_escl %s 失败: 所有端口均无响应", ip)
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
        # 不用自动跳转：部分 HP 打印机 HTTP→HTTPS 重定向但 TLS 握手失败
        r = requests.get(url, timeout=(2.0, timeout), verify=False, allow_redirects=False)
        if r.status_code in (301, 302, 307, 308):
            alt_url = url.replace("https://", "http://")
            r = requests.get(alt_url, timeout=(2.0, timeout), verify=False, allow_redirects=False)
        if r.status_code != 200:
            # ScannerCapabilities 失败 → 回退到 ScannerStatus 探测 ADF
            _probe_adf_from_status(scanner, base, timeout)
            return scanner
        root = ET.fromstring(r.text)

        # 型号 / 序列号
        for el in root.iter():
            t = _strip_ns(el)
            if t == "Model" and el.text:
                scanner.model = el.text.strip()
            if t == "SerialNumber" and el.text:
                scanner.serial = el.text.strip()

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


def get_scanner_status(escl_url: str, timeout: float = 5.0) -> dict:
    """查询扫描仪状态（Idle / Processing / ...）和 ADF 是否有纸"""
    url = escl_url.rstrip("/") + "/ScannerStatus"
    status = {"state": "Unknown", "adf_loaded": False}
    try:
        r = requests.get(url, timeout=timeout, verify=False)
        if r.status_code != 200:
            return status
        root = ET.fromstring(r.text)

        for el in root.iter():
            t = _strip_ns(el)
            if t == "State" and el.text:
                status["state"] = el.text.strip()
            if t == "AdfState" and el.text:
                status["adf_loaded"] = "Loaded" in (el.text.strip() or "")
    except requests.RequestException as e:
        logging.debug("状态查询失败: %s", e)
    except ET.ParseError as e:
        logging.debug("状态 XML 解析失败: %s", e)
    return status


# ==================== 扫描执行 ====================

NS_SCAN = "http://schemas.hp.com/scanner/escl/2011/09"
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
<scan:ScanJob xmlns:scan="{NS_SCAN}" xmlns:pwg="{NS_PWG}">
  <pwg:DocumentFormat>{doc_format}</pwg:DocumentFormat>
  <scan:InputSource>{source}</scan:InputSource>
  <scan:Intent>Document</scan:Intent>
  <scan:ScanRegions>
    <scan:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:Width>{width}</pwg:Width>
      <pwg:Height>{height}</pwg:Height>
      <scan:ScanRegionXOffset>0</scan:ScanRegionXOffset>
      <scan:ScanRegionYOffset>0</scan:ScanRegionYOffset>
    </scan:ScanRegion>
  </scan:ScanRegions>
  <scan:XResolution>{resolution}</scan:XResolution>
  <scan:YResolution>{resolution}</scan:YResolution>
  <scan:ColorMode>{color_mode}</scan:ColorMode>
  <scan:Duplex>{str(duplex).lower()}</scan:Duplex>
  {max_pages_xml}
</scan:ScanJob>"""


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


def _create_scan_job(session, scanner, resolution, color_mode, output_format, source, duplex, max_pages=1):
    """
    创建扫描任务（POST ScanJobs），返回 (base, job_uri, ext, session)。
    调用者负责关闭 session。
    """
    escl_url = scanner.escl_url
    if not escl_url:
        raise RuntimeError("未配置 eSCL URL")

    base = escl_url.rstrip("/")
    mime = FORMAT_MIME.get(output_format.lower(), "image/jpeg")
    ext = MIME_EXT.get(mime, output_format)

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

    try:
        # ADF 扫描需要设置 MaxScanPages
        max_pages = 1000 if source == "Feeder" else 1
        base, job_uri, ext = _create_scan_job(
            session, scanner, resolution, color_mode, output_format, source, duplex, max_pages=max_pages)

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

    try:
        # ADF 扫描需要设置 MaxScanPages 为较大值
        max_pages = 1000 if source == "Feeder" else 1
        base, job_uri, ext = _create_scan_job(
            session, scanner, resolution, color_mode, output_format, source, duplex, max_pages=max_pages)

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

    # 如果要求 PDF 但打印机返回 JPEG（部分机型行为），用 Pillow 转换
    if output_format.lower() == "pdf" and ext != "pdf":
        img = Image.open(io.BytesIO(data))
        pdf_path = output_path.rsplit(".", 1)[0] + ".pdf"
        img.convert("RGB").save(pdf_path, "PDF")
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
