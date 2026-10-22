"""
WSD (WS-Discovery) 引擎 — HP Scan Tool v4.0 — 并行发现局域网扫描仪
Phase 1: 仅发现，复用 escl_engine.probe_escl 获取 eSCL URL
依赖: WSDiscovery (pip install WSDiscovery)
"""

import concurrent.futures
import logging
import time
from typing import Optional
from urllib.parse import urlparse

from escl_engine import ScannerInfo, probe_escl, fetch_capabilities

logger = logging.getLogger(__name__)


# ── WSD 发现的几个预算（2026-10-22 新增）────────────────────────────────
# ★ 为什么需要：内网 WSD 服务有十几个（打印机 / 复印机 / 相机 / NAS / PC …），
#   真正是扫描仪的只有 4 台。旧实现**串行**对每个 IP 做 `probe_escl`
#   （3 秒超时）⇒ 其余 10 个不响应 eSCL 的设备各白等满 3 秒
#   ⇒ 整轮实测 **44~51 秒**，而 `discover_all_scanners` 的 join 上限只有
#   10 秒 ⇒ 用户点「刷新」要等十几秒，后台的 WSD 线程还要**继续跑 40 多秒**
#   （实测：timeout=4s 实际 43.98s；timeout=8s 实际 51.15s；mDNS 那边正常，
#   timeout=4s 实际 4.09s）。
# ⇒ 局域网上真实扫描仪的探测实测只要 0.03~0.10 秒 ⇒ 超时压到 1.5 秒绰绰有余。
#   （LAN 上真实扫描仪实测 probe 只要 0.03~0.14 秒 ⇒ 1.0 秒绰绰有余）
_WSD_PROBE_TIMEOUT = 1.0      # 单个候选 IP 的 eSCL 探测上限（秒/次请求）
_WSD_CAPS_TIMEOUT = 2.0       # 能力查询上限（真实设备实测 0.06~0.61 秒）
_WSD_EXTRA_BUDGET = 4.0        # 发现等待之外的额外余量（秒）
_WSD_PROBE_WORKERS = 16       # 并行探测线程数（内网常驻十几个 WSD 服务）


def discover_wsd(timeout: float = 4.0) -> list[ScannerInfo]:
    """
    通过 WS-Discovery 发现局域网扫描仪。
    返回 ScannerInfo 列表（已探测 eSCL 能力）。
    WSDiscovery 不可用时返回空列表。

    ★ 2026-10-22：**并行**探测 + 整体预算（修「点刷新要等十几秒」）。
      旧实现对 WSD 发现的每个 IP **串行**做 `probe_escl`（3 秒超时），
      而内网 WSD 服务里只有少数是扫描仪 ⇒ 整轮 44~51 秒（实测）。
      现在：① 先把候选 IP 收集齐（纯本地、不触网）；
            ② **并行**探测（8 线程，每 IP 1.5 秒上限）；
            ③ 整体预算 `timeout + 4s`，超时就用**已找到的结果**收尾。
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

        # ── ① 收集候选 IP（纯本地，不触网）──
        candidates = []          # [(ip, svc)] 保持发现顺序 ⇒ 结果顺序稳定
        seen_ips = set()
        for svc in services:
            try:
                xaddrs = svc.getXAddrs()
                for xaddr in xaddrs:
                    ip = _extract_ip(xaddr)
                    if ip and ip not in seen_ips:
                        seen_ips.add(ip)
                        candidates.append((ip, svc))
            except Exception as e:
                logger.debug("解析 WSD 服务失败: %s", e)
                continue

        # ── ② 并行探测（探测要触网 ⇒ 慢 ⇒ 必须并行）──
        def _probe_one(item):
            ip, svc = item
            try:
                return _build_scanner_from_wsd(
                    ip, svc, probe_timeout=_WSD_PROBE_TIMEOUT,
                    caps_timeout=_WSD_CAPS_TIMEOUT, wake_retry=False)
            except Exception as e:
                logger.debug("WSD 探测 %s 失败: %s", ip, e)
                return None

        budget = timeout + _WSD_EXTRA_BUDGET
        by_ip = {}
        if candidates:
            with concurrent.futures.ThreadPoolExecutor(
                    # ★ 线程数**按候选数收敛**：内网十几个候选时才开满，
                    #   只有一两个候选时不白开十几个线程（台账 §12.3 的
                    #   socket/句柄阈值线索之一）。
                    max_workers=max(1, min(len(candidates),
                                           _WSD_PROBE_WORKERS))) as pool:
                futs = {pool.submit(_probe_one, it): it[0]
                        for it in candidates}
                done, _not_done = concurrent.futures.wait(futs, timeout=budget)
                for fut in done:
                    try:
                        sc = fut.result()
                    except Exception:
                        sc = None
                    if sc is not None:
                        by_ip[sc.ip] = sc
                if len(done) < len(futs):
                    logger.info("WSD 探测超出预算 %.1fs：用已找到的 %d 台结果收尾",
                                budget, len(by_ip))

        # 按候选顺序输出（线程完成顺序是不确定的 ⇒ 必须按 IP 归位）
        for ip, _svc in candidates:
            if ip in by_ip:
                results.append(by_ip[ip])

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


def _build_scanner_from_wsd(ip: str, svc,
                            probe_timeout: float = 3.0,
                            caps_timeout: float = 3.0,
                            wake_retry: bool = True) -> Optional[ScannerInfo]:
    """从 WSD 服务构建 ScannerInfo，并探测 eSCL 能力"""
    # 探测 eSCL：probe_escl 内部候选已含 http:80 与 https:443，
    # **不需要**再补一轮 `port=443` —— 那一轮的请求 URL 集合与本轮
    # 完全相同（port 形参此前被忽略），每台不可达设备多 4 次 HTTP 尝试
    # + 最多约 3 秒延迟。probe_escl 的 port 形参 2026-10-21 起真正生效，
    # 非标准端口由调用方显式传入即可。
    # ★ 2026-10-22：批量探测时 wake_retry=False ⇒ 不做唤醒重试
    #   （单 IP 最高 7.5 秒 ⇒ 13 个非扫描仪 IP 串行就是 44 秒）
    escl_url = probe_escl(ip, timeout=probe_timeout, wake_retry=wake_retry)

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
        scanner = fetch_capabilities(scanner, timeout=caps_timeout)
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
        # ★ 2026-10-22：原先是 `max(timeout + 2, 10)` —— 那个**下限 10 秒**
        #   是硬编码的 ⇒ 哪怕调用方只给 3 秒，join 也要等满 10 秒
        #   ⇒ 用户点「刷新」**每次至少等 10 秒**（实测：timeout=3 实际 10.1s）。
        #   实测两条链路的真实耗时：mDNS = timeout + 0.1s、WSD = timeout + 2.1s
        #   ⇒ 改成 `timeout + 4`（留足余量，又不被下限拖住）。
        t.join(timeout + 4)

    # 按 IP 去重：同一 IP 的多条发现结果**合并**（escl_url / model / serial
    # 等取并集），不再"先到者胜"（线程完成顺序不定 ⇒ 可能丢掉 eSCL URL）
    unique = _merge_by_ip(results)

    logger.info(f"discover_all_scanners 完成: 共 {len(unique)} 台 (去重前 {len(results)} 台)")
    return unique
