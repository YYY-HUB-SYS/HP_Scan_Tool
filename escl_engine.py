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
    "execute_scan", "execute_multipage_scan", "scan_to_file",
    "apply_exposure", "auto_exposure", "manual_exposure",
    "FORMAT_MIME", "MIME_EXT",
]


def _strip_ns(el) -> str:
    """剥离 XML 元素的 namespace 前缀，返回本地标签名。"""
    t = el.tag if hasattr(el, "tag") else str(el)
    return t.split("}")[-1] if "}" in t else t


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
    返回修改后的副本，不修改传入的 scanner 对象。
    """
    if not scanner.escl_url:
        return copy.copy(scanner)

    scanner = copy.copy(scanner)
    url = scanner.escl_url.rstrip("/") + "/ScannerCapabilities"
    try:
        r = requests.get(url, timeout=timeout, verify=False)
        if r.status_code != 200:
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
    "image/bmp": "bmp",
    "application/pdf": "pdf",
    "application/octet-stream": "jpg",
}


def _stretch_channel(ch_img):
    """对单通道做 1%-99% 直方图拉伸，返回拉伸后的通道图像。"""
    hist = ch_img.histogram()
    total = sum(hist)
    lo, hi, acc = 0, 255, 0
    for i, count in enumerate(hist):
        acc += count
        if acc >= total * 0.01 and lo == 0 and i > 0:
            lo = i - 1
        if acc >= total * 0.99:
            hi = i
            break
    if hi <= lo:
        return ch_img
    scale = 255.0 / (hi - lo)
    lut = [max(0, min(255, int((v - lo) * scale))) for v in range(256)]
    return ch_img.point(lut)


def _gamma_lut(gamma: float) -> list:
    """生成 gamma 校正 LUT。gamma > 1 变亮，< 1 变暗。"""
    inv = 1.0 / gamma
    return [max(0, min(255, int(255 * ((v / 255) ** inv)))) for v in range(256)]


def _shadow_highlight_lut(shadows: int, highlights: int) -> list:
    """
    阴影/高光分离 LUT。
    shadows: -100~100 (0=不变, 正=提亮暗部, 负=压暗暗部)
    highlights: -100~100 (0=不变, 正=提亮亮部, 负=压暗亮部)
    """
    lut = []
    for v in range(256):
        # 阴影影响 0-127 区间，高光影响 128-255 区间
        if v < 128:
            # 阴影调整：用正弦曲线平滑过渡
            t = v / 127.0
            adj = shadows * (1.0 - t) * 0.5
        else:
            # 高光调整
            t = (v - 128) / 127.0
            adj = highlights * t * 0.5
        lut.append(max(0, min(255, int(v + adj))))
    return lut


def apply_exposure(img: Image.Image, mode: str = "off",
                   brightness: int = 0, contrast: int = 0,
                   gamma: float = 1.0, shadows: int = 0, highlights: int = 0,
                   channel_gains: tuple = (1.0, 1.0, 1.0),
                   mime: str = "image/jpeg") -> Image.Image:
    """
    统一曝光后处理入口。
    mode: "off" | "auto" | "manual"
    处理顺序: channel_gains → shadows/highlights → gamma → brightness/contrast → auto stretch
    直接操作 PIL Image 对象，避免 bytes 序列化往返。
    """
    if mode == "off" or img.mode not in ("RGB", "L"):
        return img

    # auto 模式：只做直方图拉伸
    if mode == "auto":
        if img.mode == "RGB":
            return Image.merge("RGB", tuple(_stretch_channel(ch) for ch in img.split()))
        return _stretch_channel(img)

    # manual 模式：按顺序叠加所有调整
    is_rgb = img.mode == "RGB"

    # Step 1: RGB 通道增益（修复偏色）
    if is_rgb and channel_gains != (1.0, 1.0, 1.0):
        channels = img.split()
        adjusted = []
        for ch, gain in zip(channels, channel_gains):
            if gain != 1.0:
                lut = [max(0, min(255, int(v * gain))) for v in range(256)]
                adjusted.append(ch.point(lut))
            else:
                adjusted.append(ch)
        img = Image.merge("RGB", tuple(adjusted))

    # Step 2: 阴影/高光分离
    if shadows != 0 or highlights != 0:
        lut = _shadow_highlight_lut(shadows, highlights)
        img = img.point(lut)

    # Step 3: Gamma 校正
    if gamma != 1.0:
        lut = _gamma_lut(gamma)
        img = img.point(lut)

    # Step 4: 亮度/对比度
    if brightness != 0 or contrast != 0:
        factor = (259 * (contrast + 255)) / (255 * (259 - contrast)) if contrast != 0 else 1.0
        lut = [max(0, min(255, int(factor * (v - 128) + 128 + brightness))) for v in range(256)]
        img = img.point(lut)

    return img


def _img_to_bytes(img: Image.Image, mime: str = "image/jpeg") -> bytes:
    """PIL Image → bytes，按 MIME 选择格式。"""
    buf = io.BytesIO()
    fmt = "JPEG" if mime == "image/jpeg" else "PNG"
    img.save(buf, fmt)
    return buf.getvalue()


def auto_exposure(data: bytes, mime: str = "image/jpeg") -> bytes:
    """自动曝光后处理 — 直方图拉伸。（向后兼容的 bytes 接口）"""
    try:
        img = Image.open(io.BytesIO(data))
        result = apply_exposure(img, mode="auto", mime=mime)
        return _img_to_bytes(result, mime)
    except Exception as e:
        logging.debug("自动曝光处理失败: %s", e)
        return data


def manual_exposure(data: bytes, brightness: int = 0, contrast: int = 0,
                    mime: str = "image/jpeg") -> bytes:
    """手动曝光后处理 — 亮度/对比度调整。（向后兼容的 bytes 接口）"""
    if brightness == 0 and contrast == 0:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        result = apply_exposure(img, mode="manual", brightness=brightness,
                                contrast=contrast, mime=mime)
        return _img_to_bytes(result, mime)
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

    try:
        # Step 1: 创建扫描任务
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

        # 补全 URL
        if not job_uri.startswith("http"):
            host = base.split("://")[1].split("/")[0]
            job_uri = f"http://{host}{job_uri}"

        # Step 2: 轮询 NextDocument
        start = time.time()
        nd_url = job_uri.rstrip("/") + "/NextDocument"
        while time.time() - start < timeout:
            try:
                r = session.get(nd_url, timeout=10)
                if r.status_code == 200:
                    return r.content, ext
                elif r.status_code == 503:
                    pass  # 扫描进行中
            except requests.RequestException:
                pass
            time.sleep(0.5)

        raise RuntimeError("扫描超时：未能在限定时间内获取扫描数据")
    finally:
        session.close()


def execute_multipage_scan(
    scanner: ScannerInfo,
    resolution: int = 300,
    color_mode: str = "RGB24",
    output_format: str = "jpg",
    source: str = "Platen",
    duplex: bool = False,
    timeout: float = 300.0,
    page_timeout: float = 60.0,
    progress_callback=None,
) -> list[tuple[bytes, str]]:
    """
    执行多页 ADF 扫描，返回 [(数据, 扩展名), ...]。
    循环获取 NextDocument 直到 HTTP 404（无更多页面）。
    progress_callback(page_num, total_so_far) 可选，用于 UI 进度更新。
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
    session = requests.Session()
    session.verify = False

    try:
        # Step 1: 创建扫描任务
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

        # Step 2: 循环获取页面
        pages = []
        nd_url = job_uri.rstrip("/") + "/NextDocument"
        overall_start = time.time()

        while time.time() - overall_start < timeout:
            # 等待当前页面就绪
            page_start = time.time()
            while time.time() - page_start < page_timeout:
                try:
                    r = session.get(nd_url, timeout=10)
                    if r.status_code == 200:
                        pages.append((r.content, ext))
                        if progress_callback:
                            progress_callback(len(pages), len(pages))
                        break  # 获取成功，进入下一页
                    elif r.status_code == 404:
                        # 无更多页面
                        return pages
                    elif r.status_code == 503:
                        pass  # 扫描进行中，继续等待
                except requests.RequestException:
                    pass
                time.sleep(0.5)
            else:
                # 单页超时
                if pages:
                    break  # 已有页面，结束扫描
                raise RuntimeError("扫描超时：未能获取第一页数据")

        return pages
    finally:
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
