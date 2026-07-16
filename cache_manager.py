"""
磁盘缓存管理器 — HP Scan Tool v3.3
扫描数据直接写入磁盘缓存，预览时加载缩略图，确认后复制到目标并清理。

缓存结构:
  {cache_root}/
    {job_id}/
      page_001.jpg
      page_002.jpg
      ...

清理策略:
  - 上限可配置（默认 1024MB）
  - 达到 95% 触发线时自动 FIFO 清除 150MB
  - 启动时清理超过 24 小时的残留缓存
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# 默认缓存配置
_DEFAULTS = {
    "cache_limit_mb": 1024,
    "trigger_ratio": 0.95,
    "cleanup_mb": 150,
    "stale_hours": 24,
}


def _cache_config_path():
    """缓存配置文件路径（与 scan_config.json 同级）"""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(app_dir, "cache_config.json")


def _default_cache_root():
    """默认缓存根目录: ~/Documents/HP_Scans/.cache/"""
    return os.path.join(os.path.expanduser("~"), "Documents", "HP_Scans", ".cache")


def load_cache_config() -> dict:
    """加载缓存配置，缺失字段用默认值填充"""
    path = _cache_config_path()
    cfg = dict(_DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
        except Exception as e:
            logger.debug("缓存配置加载失败，使用默认值: %s", e)
    return cfg


def save_cache_config(cfg: dict):
    """保存缓存配置"""
    path = _cache_config_path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("缓存配置保存失败: %s", e)


def cache_root() -> str:
    """获取缓存根目录（从配置读取，不存在则创建）"""
    cfg = load_cache_config()
    root = cfg.get("cache_root", _default_cache_root())
    os.makedirs(root, exist_ok=True)
    return root


def job_dir(job_id: str) -> str:
    """获取指定 job 的缓存目录（不存在则创建）"""
    d = os.path.join(cache_root(), job_id)
    os.makedirs(d, exist_ok=True)
    return d


def write_page(job_id: str, page_num: int, data: bytes, ext: str) -> str:
    """
    将扫描页写入缓存。
    返回缓存文件路径。
    """
    d = job_dir(job_id)
    path = os.path.join(d, f"page_{page_num:03d}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    logger.debug("缓存写入: %s (%d bytes)", path, len(data))

    # 写入后检查是否需要清理
    _maybe_evict()

    return path


def read_page(job_id: str, page_num: int, ext: str) -> bytes:
    """从缓存读取指定页的数据"""
    d = os.path.join(cache_root(), job_id)
    path = os.path.join(d, f"page_{page_num:03d}.{ext}")
    with open(path, "rb") as f:
        return f.read()


def list_pages(job_id: str) -> list[tuple[int, str, str]]:
    """
    列出指定 job 的所有缓存页。
    返回 [(page_num, ext, path), ...] 按页码排序。
    """
    d = os.path.join(cache_root(), job_id)
    if not os.path.isdir(d):
        return []

    pages = []
    for fname in os.listdir(d):
        if fname.startswith("page_") and "." in fname:
            try:
                num = int(fname[5:8])
                ext = fname.rsplit(".", 1)[1]
                pages.append((num, ext, os.path.join(d, fname)))
            except (ValueError, IndexError):
                continue
    pages.sort()
    return pages


def remove_job(job_id: str):
    """删除指定 job 的全部缓存"""
    import shutil
    d = os.path.join(cache_root(), job_id)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        logger.debug("缓存清除: %s", d)


def cache_size_bytes() -> int:
    """计算当前缓存总大小（字节）"""
    root = cache_root()
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def cache_size_mb() -> float:
    """当前缓存大小（MB）"""
    return cache_size_bytes() / (1024 * 1024)


def _maybe_evict():
    """检查缓存大小，接近触发线时执行 FIFO 清理"""
    cfg = load_cache_config()
    limit_bytes = cfg["cache_limit_mb"] * 1024 * 1024
    trigger_bytes = limit_bytes * cfg["trigger_ratio"]
    cleanup_bytes = cfg["cleanup_mb"] * 1024 * 1024

    current = cache_size_bytes()
    if current < trigger_bytes:
        return

    logger.info("缓存 %.1fMB 超过触发线 %.1fMB，开始清理...",
                current / (1024 * 1024), trigger_bytes / (1024 * 1024))
    _evict_oldest(cleanup_bytes)


def _evict_oldest(target_bytes: int):
    """FIFO 清理：按文件修改时间从旧到新删除，直到释放 target_bytes"""
    root = cache_root()
    files = []
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                files.append((os.path.getmtime(fp), os.path.getsize(fp), fp))
            except OSError:
                pass

    files.sort()  # 最旧的在前

    freed = 0
    for mtime, size, fp in files:
        if freed >= target_bytes:
            break
        try:
            os.remove(fp)
            freed += size
            logger.debug("清理缓存文件: %s (%.1fMB)", fp, size / (1024 * 1024))
        except OSError as e:
            logger.debug("清理失败: %s: %s", fp, e)

    # 清理空目录
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if not filenames and not dirnames and dirpath != root:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    logger.info("缓存清理完成，释放 %.1fMB", freed / (1024 * 1024))


def cleanup_stale():
    """启动时清理超过 stale_hours 的残留缓存（防崩溃遗留）"""
    cfg = load_cache_config()
    stale_seconds = cfg["stale_hours"] * 3600
    root = cache_root()
    now = time.time()

    for entry in os.listdir(root):
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        try:
            mtime = os.path.getmtime(d)
            if now - mtime > stale_seconds:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
                logger.debug("清理过期缓存: %s (age: %.1fh)", entry,
                             (now - mtime) / 3600)
        except OSError:
            pass
