"""
eSCL 监听模式引擎 — HP Scan Tool v4.0 — 等待打印机面板触发扫描

部分 HP 打印机支持用户在面板上选择「扫描到计算机」，
打印机主动创建扫描任务，计算机只需轮询获取。

此模块实现监听模式：
1. 创建扫描任务（POST ScanJobs）
2. 轮询等待打印机完成扫描（GET NextDocument）
3. 当用户在面板上选择扫描时，打印机自动完成任务
"""

import logging
import time

import requests

from escl_engine import ScannerInfo, _create_scan_job

logger = logging.getLogger(__name__)


def listen_for_scan(
    scanner: ScannerInfo,
    resolution: int = 300,
    color_mode: str = "RGB24",
    output_format: str = "jpg",
    source: str = "Platen",
    duplex: bool = False,
    wait_timeout: float = 300.0,
    poll_interval: float = 2.0,
    progress_callback=None,
    cancel_event=None,
) -> tuple[bytes, str] | None:
    """
    监听模式：等待打印机面板触发扫描。

    创建扫描任务后，不立即获取文档，而是等待用户在打印机面板
    上选择扫描目标并启动扫描。当打印机完成扫描后，自动获取文档。

    Args:
        scanner: 扫描仪信息
        resolution: 分辨率 DPI
        color_mode: 颜色模式
        output_format: 输出格式
        source: 扫描来源（Platen/Feeder）
        duplex: 是否双面
        wait_timeout: 等待超时秒数（默认 5 分钟）
        poll_interval: 轮询间隔秒数
        progress_callback: 回调函数(status_msg)
        cancel_event: threading.Event 取消事件

    Returns:
        (数据, 扩展名) 如果扫描成功，None 如果超时或取消
    """
    session = requests.Session()
    session.verify = False
    job_uri = None

    try:
        if progress_callback:
            progress_callback("正在创建监听任务...")

        # 创建扫描任务
        # ADF 必须显式放大 MaxScanPages，否则 _build_scan_job_xml 只写
        # <scan:MaxScanPages>1</scan:MaxScanPages>，打印机只吐 1 页
        max_pages = 1000 if source == "Feeder" else 1
        base, job_uri, ext = _create_scan_job(
            session, scanner, resolution, color_mode, output_format, source, duplex,
            max_pages=max_pages, cancel_event=cancel_event
        )

        if progress_callback:
            progress_callback("等待打印机面板触发扫描...")

        # 轮询等待扫描完成
        start = time.time()
        nd_url = job_uri.rstrip("/") + "/NextDocument"

        while time.time() - start < wait_timeout:
            # 检查取消
            if cancel_event and cancel_event.is_set():
                logger.info("监听模式已取消")
                return None

            try:
                r = session.get(nd_url, timeout=10)
                if r.status_code == 200:
                    if progress_callback:
                        progress_callback("扫描完成，正在获取数据...")
                    return r.content, ext
                elif r.status_code == 503:
                    # 扫描进行中或等待用户操作
                    pass
                elif r.status_code == 404:
                    # 任务已取消或过期
                    logger.info("监听任务已取消或过期")
                    return None
            except requests.RequestException:
                pass

            # 分段睡眠以便及时响应取消
            for _ in range(int(poll_interval * 5)):
                if cancel_event and cancel_event.is_set():
                    return None
                time.sleep(0.2)

        logger.warning("监听模式超时：%.0f 秒内未收到扫描", wait_timeout)
        return None

    finally:
        # ★ 清理打印机上的扫描任务（与 execute_scan 对齐）：
        #   监听被取消/超时后，打印机上会残留一个未完成的 ScanJob；
        #   紧接着的普通扫描会撞 409（提示"打印机上有一个未完成的扫描任务"，
        #   其实是自己上次监听留下的），且实测可能很久都不被回收。
        #   job_uri 可能未绑定（创建任务就失败）⇒ 用 None 哨兵，不抛二次异常。
        if job_uri:
            try:
                session.delete(job_uri, timeout=3)
            except Exception as e:
                logger.debug("删除监听扫描任务失败（忽略）: %s", e)
        session.close()


def listen_for_multipage_scan(
    scanner: ScannerInfo,
    resolution: int = 300,
    color_mode: str = "RGB24",
    output_format: str = "jpg",
    source: str = "Feeder",
    duplex: bool = False,
    wait_timeout: float = 600.0,
    page_timeout: float = 120.0,
    poll_interval: float = 2.0,
    progress_callback=None,
    cancel_event=None,
) -> list[tuple[bytes, str]] | None:
    """
    监听模式（多页）：等待打印机面板触发 ADF 扫描。

    Args:
        scanner: 扫描仪信息
        resolution: 分辨率 DPI
        color_mode: 颜色模式
        output_format: 输出格式
        source: 扫描来源（应为 Feeder）
        duplex: 是否双面
        wait_timeout: 总等待超时秒数
        page_timeout: 单页等待超时秒数
        poll_interval: 轮询间隔秒数
        progress_callback: 回调函数(status_msg, page_num)
        cancel_event: threading.Event 取消事件

    Returns:
        [(数据, 扩展名), ...] 如果扫描成功，None 如果超时或取消
    """
    session = requests.Session()
    session.verify = False
    job_uri = None

    try:
        if progress_callback:
            progress_callback("正在创建监听任务...", 0)

        # 创建扫描任务
        # ADF 必须显式放大 MaxScanPages，否则 _build_scan_job_xml 只写
        # <scan:MaxScanPages>1</scan:MaxScanPages>，打印机只吐 1 页
        max_pages = 1000 if source == "Feeder" else 1
        base, job_uri, ext = _create_scan_job(
            session, scanner, resolution, color_mode, output_format, source, duplex,
            max_pages=max_pages, cancel_event=cancel_event
        )

        if progress_callback:
            progress_callback("等待打印机面板触发扫描...", 0)

        # 循环获取页面
        pages = []
        nd_url = job_uri.rstrip("/") + "/NextDocument"
        overall_start = time.time()

        while time.time() - overall_start < wait_timeout:
            # 检查取消
            if cancel_event and cancel_event.is_set():
                logger.info("监听模式已取消")
                return pages if pages else None

            page_start = time.time()
            while time.time() - page_start < page_timeout:
                if cancel_event and cancel_event.is_set():
                    return pages if pages else None

                try:
                    r = session.get(nd_url, timeout=10)
                    if r.status_code == 200:
                        pages.append((r.content, ext))
                        if progress_callback:
                            progress_callback(f"已获取 {len(pages)} 页", len(pages))
                        time.sleep(0.3)
                        break
                    elif r.status_code == 404:
                        # 无更多页面
                        return pages if pages else None
                    elif r.status_code == 503:
                        pass
                except requests.RequestException:
                    pass

                # 分段睡眠以便及时响应取消
                for _ in range(int(poll_interval * 5)):
                    if cancel_event and cancel_event.is_set():
                        return pages if pages else None
                    time.sleep(0.2)
            else:
                if pages:
                    break
                return None

        return pages if pages else None

    finally:
        # ★ 同 listen_for_scan：监听取消/超时后必须删掉打印机上的 ScanJob，
        #   否则残留 job 会让紧接着的普通扫描撞 409（残留记录可能长期不回收）。
        if job_uri:
            try:
                session.delete(job_uri, timeout=3)
            except Exception as e:
                logger.debug("删除监听扫描任务失败（忽略）: %s", e)
        session.close()
