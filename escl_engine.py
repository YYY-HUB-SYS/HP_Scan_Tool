"""
eSCL (AirScan) 扫描引擎 — 合并版
参考：
  - OpenScanHub: 多协议架构、ADF 检测
  - node-hp-scan-to: 完整 eSCL 协议交互流程(ScannerCapabilities → ScannerStatus → ScanJobs → NextDocument)
  - photo-scan-split: 极简 mDNS 发现
"""

import io
import logging
import time
import xml.etree.ElementTree as ET
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
    "execute_scan", "scan_to_file", "auto_exposure",
    "FORMAT_MIME", "MIME_EXT",
]


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
    base = f"http://{ip}:{port}"

    # 优先尝试 /eSCL/ 路径
    for path in ("/eSCL/ScannerStatus", "/ScannerStatus"):
        try:
            r = requests.get(f"{base}{path}", timeout=timeout, verify=False)
            if r.status_code == 200:
                if "/eSCL" in path:
                    return f"{base}/eSCL/"
                return f"{base}/"
        except Exception:
            continue

    return None


# ==================== 能力查询 (node-hp-scan-to 协议) ====================

def fetch_capabilities(scanner: ScannerInfo, timeout: float = 5.0) -> ScannerInfo:
    """
    从 /eSCL/ScannerCapabilities 解析扫描仪全部能力。
    就地修改传入的 scanner 对象并返回同一引用。
    """
    if not scanner.escl_url:
        return scanner

    url = scanner.escl_url.rstrip("/") + "/ScannerCapabilities"
    try:
        r = requests.get(url, timeout=timeout, verify=False)
        if r.status_code != 200:
            return scanner
        root = ET.fromstring(r.text)

        def tag(el):
            return el.tag.split("}")[-1] if "}" in el.tag else el.tag

        # 型号 / 序列号
        for el in root.iter():
            t = tag(el)
            if t == "Model" and el.text:
                scanner.model = el.text.strip()
            if t == "SerialNumber" and el.text:
                scanner.serial = el.text.strip()

        # 平板 / 输稿器
        platen_sections = []
        adf_sections = []

        for child in root:
            t = tag(child)
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
                if tag(el) == "Duplex" and el.text and el.text.strip().lower() == "true":
                    scanner.has_duplex = True

        if source_section is None:
            source_section = root

        # 最大尺寸
        for el in source_section.iter():
            t = tag(el)
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
            t = tag(el)
            if t in ("DiscreteResolutions", "SupportedResolutions"):
                for child in el:
                    if tag(child) == "Width":
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
            t = tag(el)
            if t in ("ColorModes", "SupportedColorModes"):
                for child in el:
                    if child.text and child.text.strip():
                        color_modes.append(child.text.strip())
        if color_modes:
            scanner.color_modes = color_modes

        # 格式
        formats = []
        for el in source_section.iter():
            t = tag(el)
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

        def tag(el):
            return el.tag.split("}")[-1] if "}" in el.tag else el.tag

        for el in root.iter():
            t = tag(el)
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
) -> str:
    """构建 POST /eSCL/ScanJobs 请求体，严格参照 node-hp-scan-to 抓包格式"""
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
    "application/pdf": "pdf",
    "application/octet-stream": "jpg",
}


def auto_exposure(data: bytes, mime: str = "image/jpeg") -> bytes:
    """
    自动曝光后处理 — 直方图拉伸。
    分析图像各通道像素分布，取 1%-99% 分位区间线性拉伸到 0-255，
    有效去除扫描件灰底、提升文字对比度。
    """
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            return data

        if img.mode == "RGB":
            # 分通道处理
            channels = img.split()
            stretched = []
            for ch in channels:
                hist = ch.histogram()  # 256 个桶
                total = sum(hist)
                # 找 1% 和 99% 分位
                lo, hi, acc = 0, 255, 0
                for i, count in enumerate(hist):
                    acc += count
                    if acc >= total * 0.01 and lo == 0 and i > 0:
                        lo = i - 1
                    if acc >= total * 0.99:
                        hi = i
                        break
                # 构建 LUT
                if hi <= lo:
                    stretched.append(ch)
                else:
                    scale = 255.0 / (hi - lo)
                    lut = [max(0, min(255, int((v - lo) * scale))) for v in range(256)]
                    stretched.append(ch.point(lut))
            img = Image.merge("RGB", stretched)
        else:
            # 灰度图
            hist = img.histogram()
            total = sum(hist)
            lo, hi, acc = 0, 255, 0
            for i, count in enumerate(hist):
                acc += count
                if acc >= total * 0.01 and lo == 0 and i > 0:
                    lo = i - 1
                if acc >= total * 0.99:
                    hi = i
                    break
            if hi > lo:
                scale = 255.0 / (hi - lo)
                lut = [max(0, min(255, int((v - lo) * scale))) for v in range(256)]
                img = img.point(lut)

        buf = io.BytesIO()
        fmt = "JPEG" if mime == "image/jpeg" else "PNG"
        img.save(buf, fmt)
        return buf.getvalue()
    except Exception as e:
        logging.debug("自动曝光处理失败: %s", e)
        return data  # 失败时返回原始数据


def manual_exposure(data: bytes, brightness: int = 0, contrast: int = 0,
                    mime: str = "image/jpeg") -> bytes:
    """
    手动曝光后处理 — 亮度/对比度调整。
    brightness: -100 ~ 100（0 = 不变）
    contrast:   -100 ~ 100（0 = 不变）
    """
    if brightness == 0 and contrast == 0:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            return data

        # 构建 LUT：先对比度，后亮度
        factor = (259 * (contrast + 255)) / (255 * (259 - contrast)) if contrast != 0 else 1.0
        lut = []
        for v in range(256):
            val = int(factor * (v - 128) + 128 + brightness)
            lut.append(max(0, min(255, val)))

        img = img.point(lut)
        buf = io.BytesIO()
        fmt = "JPEG" if mime == "image/jpeg" else "PNG"
        img.save(buf, fmt)
        return buf.getvalue()
    except Exception as e:
        logging.debug("手动曝光处理失败: %s", e)
        return data


def execute_scan(
    scanner: ScannerInfo,
    resolution: int = 300,
    color_mode: str = "RGB24",
    output_format: str = "jpg",
    source: str = "Platen",
    duplex: bool = False,
    timeout: float = 120.0,
) -> tuple[bytes, str]:
    """
    执行一次完整扫描，返回 (数据, 实际扩展名)。
    流程: POST ScanJobs → 轮询 JobState → GET NextDocument
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
    )

    headers = {"Content-Type": "application/xml"}

    # 使用 Session 复用 TCP 连接（轮询 NextDocument 时避免反复握手）
    session = requests.Session()
    session.verify = False

    # Step 1: 创建扫描任务
    r = session.post(f"{base}/ScanJobs", data=xml_body, headers=headers, timeout=30)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"创建扫描任务失败 HTTP {r.status_code}: {r.text[:300]}")

    job_uri = r.headers.get("Location", "")
    if not job_uri:
        # 从响应体解析
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

    # 补全 URL
    if not job_uri.startswith("http"):
        host = base.split("://")[1].split("/")[0]
        job_uri = f"http://{host}{job_uri}"

    # Step 2: 轮询 NextDocument（部分 HP 机型不支持 job URI 轮询，永远 404）
    start = time.time()
    nd_url = job_uri.rstrip("/") + "/NextDocument"
    while time.time() - start < timeout:
        try:
            r = session.get(nd_url, timeout=10)
            if r.status_code == 200:
                session.close()
                return r.content, ext
            elif r.status_code == 503:
                pass  # 扫描进行中
            # 404/其他: 继续等待
        except requests.RequestException:
            pass
        time.sleep(0.5)

    session.close()
    raise TimeoutError(f"扫描超时（{timeout:.0f}s），请确认纸张已放入并重试")


def scan_to_file(
    scanner: ScannerInfo,
    output_path: str,
    resolution: int = 300,
    color_mode: str = "RGB24",
    output_format: str = "jpg",
    source: str = "Platen",
    duplex: bool = False,
    timeout: float = 120.0,
    exposure_mode: str = "off",
    brightness: int = 0,
    contrast: int = 0,
) -> str:
    """扫描并保存到文件，返回文件路径。exposure_mode: off/auto/manual"""
    data, ext = execute_scan(
        scanner=scanner,
        resolution=resolution,
        color_mode=color_mode,
        output_format=output_format,
        source=source,
        duplex=duplex,
        timeout=timeout,
    )

    # 曝光后处理
    mime = FORMAT_MIME.get(ext, "image/jpeg")
    if exposure_mode == "auto":
        data = auto_exposure(data, mime)
    elif exposure_mode == "manual":
        data = manual_exposure(data, brightness, contrast, mime)

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
