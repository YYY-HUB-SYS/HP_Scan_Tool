"""
WSD (WS-Discovery) 引擎 — HP Scan Tool v4.0 — 并行发现局域网扫描仪
Phase 1: 仅发现，复用 escl_engine.probe_escl 获取 eSCL URL
依赖: WSDiscovery (pip install WSDiscovery)
"""

import logging
from typing import Optional
from urllib.parse import urlparse

from escl_engine import ScannerInfo, probe_escl, fetch_capabilities

logger = logging.getLogger(__name__)

# WSD 常量
WSD_MULTICAST_ADDR = "239.255.255.250"
WSD_PORT = 3702


def discover_wsd(timeout: float = 4.0) -> list[ScannerInfo]:
    """
    通过 WS-Discovery 发现局域网扫描仪。
    返回 ScannerInfo 列表（已探测 eSCL 能力）。
    WSDiscovery 不可用时返回空列表。
    """
    try:
        from wsdiscovery import WSDiscovery
    except ImportError:
        logger.debug("WSDiscovery 库未安装，跳过 WSD 发现")
        return []

    results = []
    wsd = None
    try:
        wsd = WSDiscovery()
        wsd.start()

        # 发送 Probe，等待 ProbeMatch
        services = wsd.searchServices(timeout=timeout)

        seen_ips = set()
        for svc in services:
            try:
                xaddrs = svc.getXAddrs()
                for xaddr in xaddrs:
                    ip = _extract_ip(xaddr)
                    if ip and ip not in seen_ips:
                        seen_ips.add(ip)
                        scanner = _build_scanner_from_wsd(ip, svc)
                        if scanner:
                            results.append(scanner)
            except Exception as e:
                logger.debug("解析 WSD 服务失败: %s", e)
                continue

    except Exception as e:
        logger.debug("WSD 发现失败: %s", e)
    finally:
        if wsd:
            try:
                wsd.stop()
            except Exception:
                pass

    return results


def _extract_ip(xaddr: str) -> Optional[str]:
    """从 XAddr URL 中提取 IP 地址"""
    try:
        parsed = urlparse(xaddr)
        host = parsed.hostname
        if host:
            # 简单验证是否为 IPv4
            parts = host.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                return host
    except Exception:
        pass
    return None


def _build_scanner_from_wsd(ip: str, svc) -> Optional[ScannerInfo]:
    """从 WSD 服务构建 ScannerInfo，并探测 eSCL 能力"""
    # 探测 eSCL：probe_escl 内部候选已含 http:80 与 https:443，
    # **不需要**再补一轮 `port=443` —— 那一轮的请求 URL 集合与本轮
    # 完全相同（port 形参此前被忽略），每台不可达设备多 4 次 HTTP 尝试
    # + 最多约 3 秒延迟。probe_escl 的 port 形参 2026-10-21 起真正生效，
    # 非标准端口由调用方显式传入即可。
    escl_url = probe_escl(ip, timeout=3.0)

    if not escl_url:
        return None  # WSD 发现但无 eSCL，暂不纳入

    # 尝试从 WSD 服务获取设备名称
    name = ""
    try:
        # svc 可能有 getDisplayName 或类似方法
        if hasattr(svc, "getDisplayName"):
            name = svc.getDisplayName() or ""
    except Exception:
        pass

    if not name:
        name = f"WSD-Scanner-{ip}"

    scanner = ScannerInfo(
        name=name,
        ip=ip,
        port=443 if escl_url.startswith("https") else 80,
        escl_url=escl_url,
    )

    # 尝试获取更详细的能力信息
    try:
        scanner = fetch_capabilities(scanner, timeout=3.0)
    except Exception as e:
        logger.debug("WSD 扫描仪 %s 能力查询失败: %s", ip, e)

    return scanner


def _merge_scanner_fields(target: ScannerInfo, other: ScannerInfo) -> None:
    """
    把 `other` 的信息**并进** `target`（就地修改）。

    规则：
      - 字符串字段（escl_url / model / serial / name / custom_name）：
        空者被填；两者都有值时保留先到者（稳定、可预期）
      - 布尔能力（has_adf / has_duplex / has_platen / capabilities_known）：
        任一为真即真
      - 列表能力（resolutions / color_modes / formats）：取并集
      - 尺寸（max_width / max_height）：取更大者（默认值不是"没拿到"的标记）
      - port：取先到者的非零值
    """
    for attr in ("escl_url", "model", "serial", "name", "custom_name"):
        if not getattr(target, attr, "") and getattr(other, attr, ""):
            setattr(target, attr, getattr(other, attr))
    for attr in ("has_adf", "has_duplex", "has_platen", "capabilities_known"):
        if getattr(other, attr, False):
            setattr(target, attr, True)
    for attr in ("resolutions", "color_modes", "formats"):
        cur = list(getattr(target, attr, None) or [])
        for v in (getattr(other, attr, None) or []):
            if v not in cur:
                cur.append(v)
        setattr(target, attr, cur)
    for attr in ("max_width", "max_height"):
        try:
            if int(getattr(other, attr, 0) or 0) > int(getattr(target, attr, 0) or 0):
                setattr(target, attr, getattr(other, attr))
        except (TypeError, ValueError):
            pass
    if not getattr(target, "port", 0) and getattr(other, "port", 0):
        target.port = other.port


def _merge_by_ip(scanners: list) -> list:
    """
    按 IP 去重：**合并**而不是"先到者胜"。

    背景（独立核销 N-8）：mDNS 与 WSD 两条发现链路常同时发现同一台设备，
    但它们给出的信息**互补** —— mDNS 侧构造的 ScannerInfo 不带 `escl_url`，
    WSD 侧带。原实现按"先到者胜"去重，而 `results` 的顺序 = **线程完成
    顺序**（不确定）⇒ mDNS 先完成时会留下 `escl_url=''` 的那条、把 WSD
    侧探测到的 eSCL URL 丢掉 ⇒ `cli.py discover --format json` 的
    escl_url 变成空串，同一台设备两次 discover 可能给出不同结果。

    ⇒ 现在同一 IP 的多条结果取**并集**（escl_url / model / serial 等
    谁有值取谁），同时仍然只产出一条 ScannerInfo。
    """
    merged: dict = {}
    order: list = []
    for s in scanners:
        key = getattr(s, "ip", "")
        if key not in merged:
            merged[key] = s
            order.append(key)
            continue
        _merge_scanner_fields(merged[key], s)
    return [merged[k] for k in order]


def discover_all_scanners(timeout: float = 4.0) -> list[ScannerInfo]:
    """
    并行使用 mDNS + WSD 发现扫描仪，按 IP 去重。
    替代单独调用 escl_engine.discover_scanners。
    """
    import threading
    from escl_engine import discover_scanners as discover_mdns

    logger.info(f"discover_all_scanners 启动 (timeout={timeout}s)")
    results = []
    errors = []

    def _run_discover(target, name):
        try:
            result = target(timeout)
            logger.info(f"  {name} 发现 {len(result)} 台")
            results.extend(result)
        except Exception as e:
            errors.append((name, e))
            logger.warning("发现方式失败 (%s): %s", name, e)

    threads = [
        threading.Thread(target=_run_discover, args=(discover_mdns, "mDNS"), daemon=True),
        threading.Thread(target=_run_discover, args=(discover_wsd, "WSD"), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(timeout + 2, 10))

    # 按 IP 去重：同一 IP 的多条发现结果**合并**（escl_url / model / serial
    # 等取并集），不再"先到者胜"（线程完成顺序不定 ⇒ 可能丢掉 eSCL URL）
    unique = _merge_by_ip(results)

    logger.info(f"discover_all_scanners 完成: 共 {len(unique)} 台 (去重前 {len(results)} 台)")
    return unique
