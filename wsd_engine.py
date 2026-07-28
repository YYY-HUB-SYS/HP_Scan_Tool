"""
WSD (WS-Discovery) 引擎 — 并行发现局域网扫描仪
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
    # 尝试探测 eSCL
    escl_url = probe_escl(ip, timeout=3.0)
    if not escl_url:
        # 也尝试 HTTPS
        escl_url = probe_escl(ip, port=443, timeout=3.0)

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

    # 按 IP 去重（优先保留 mDNS 结果，因为通常更完整）
    seen_ips = set()
    unique = []
    for scanner in results:
        if scanner.ip not in seen_ips:
            seen_ips.add(scanner.ip)
            unique.append(scanner)

    logger.info(f"discover_all_scanners 完成: 共 {len(unique)} 台 (去重前 {len(results)} 台)")
    return unique
