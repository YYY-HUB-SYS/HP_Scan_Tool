"""
磁盘缓存管理器 — HP Scan Tool v4.0
扫描数据直接写入磁盘缓存，预览时加载缩略图，确认后复制到目标并清理。

缓存结构:
  {cache_root}/
    {job_id}/
      page_001.jpg
      page_002.jpg
      ...

清理策略:
  - 上限可配置（默认 1024MB）
  - 写入前检查，达到 95% 触发线时自动 FIFO 清除 150MB
  - FIFO 按任务目录创建时间排序，整目录删除
  - 活跃任务受保护，不会被清理
  - 已扫描未保存的任务（_pending_jobs）同样受保护，直到落盘或用户放弃
  - 显式保护的目录（_protected_dirs，如文档会话引用的 job）同样受保护
  - 启动时清理超过 24 小时的残留缓存
"""

import json
import logging
import os
import shutil
import threading
import time

import config_io
from config_io import (
    atomic_write_json as _atomic_write_json,
    config_dir,
)

logger = logging.getLogger(__name__)

# 默认缓存配置
_DEFAULTS = {
    "cache_limit_mb": 1024,
    "trigger_ratio": 0.95,
    "cleanup_mb": 150,
    "stale_hours": 24,
}

# ── 线程安全 ──
_lock = threading.Lock()
# 配置读-改-写事务的线程锁（与 _lock 分开，避免与缓存清理逻辑嵌套）
_config_lock = threading.Lock()

# ── 活跃任务集合：注册后不会被 eviction 清理 ──
# 语义：**正在扫描**（register_job → unregister_job / remove_job）
_active_jobs: set[str] = set()

# ── 待保存任务集合：已扫描、未保存 ──
# 语义：**已扫描未保存**（register_pending_job → unregister_pending_job / remove_job）
#
# 为什么单独一个集合（F-4）：
#   扫描完成那一刻，run_scan_sync 就把 job 从 _active_jobs 注销了；
#   但用户此时还在预览里没点保存，可能挂几分钟。这段时间里如果另一条
#   扫描把缓存顶过 95% 触发线，FIFO 会把这个 job 目录删掉 ⇒ 用户预览里
#   的页被静默跳过（产物少页 + 界面零提示）。
#   ⇒ "已扫描未保存"的 job 必须一直受保护，直到落盘成功或用户放弃。
_pending_jobs: set[str] = set()

# ── 显式保护目录集合：登记后不被 eviction / cleanup_stale 清理 ──
# 结构：{job 目录名: 登记时的缓存根（规范化绝对路径）}
#
# 为什么带"缓存根"这一维（而不是一个扁平的 set[str]）：
#   保护是**针对某个缓存根下的某个 job 目录**而言的。若用扁平集合，
#   一处登记的 job 名会泄漏到另一处**同名**目录上 —— 例如 pytest 换执行
#   顺序、或并行执行时，A 用例登记的 "job_a" 会让 B 用例里同名的目录
#   也被当成受保护 ⇒ 保护张冠李戴、eviction 行为随执行顺序漂移。
#   按根分组后，保护只对**登记时那个缓存根**生效；正常使用时进程内
#   只有一个缓存根 ⇒ 行为与扁平集合完全一致。
#
# 语义：protect_dir → unprotect_dir（幂等）
#
# ★ 跨进程边界（重要）：
#   _active_jobs / _pending_jobs / _protected_dirs **全部是进程内集合**，
#   进程重启后一律为空 ⇒ 本模块**不做**跨进程的持久化保护。
#   跨进程恢复由 **document_session** 负责：它加载会话时会重新
#   `protect_dir` 那些 job 目录（会话文件里记着 job 列表）。
#   本模块只保证"本进程内登记过 ⇒ 两条自动清理路径都跳过"。
_protected_dirs: dict[str, str] = {}

# ── 配置缓存：避免每次调用重读 JSON ──
_config_cache: dict | None = None
_config_cache_time: float = 0
_CONFIG_CACHE_TTL = 5.0  # 5 秒内复用

# ── 缓存大小缓存：避免每写一页都全量 os.walk 缓存根（E-3）──
# (字节数, 计算时刻)；写页 / 删目录按增量修正，TTL 过期才全量校准
_cache_size_cache: tuple[int, float] | None = None
_CACHE_SIZE_TTL = 1.5  # 1.5 秒内复用
_size_lock = threading.Lock()


def _app_dir():
    """配置目录：%APPDATA%\\HP_Scan_Tool\\（Windows 标准应用数据目录）"""
    return config_dir()


def _cache_config_path():
    """缓存配置文件路径（与 scan_config.json 同级）"""
    return os.path.join(_app_dir(), "cache_config.json")


def _default_cache_root():
    """默认缓存根目录: ~/Documents/HP_Scans/.cache/"""
    return os.path.join(os.path.expanduser("~"), "Documents", "HP_Scans", ".cache")


def load_cache_config() -> dict:
    """加载缓存配置，缺失字段用默认值填充（带 5 秒缓存）"""
    global _config_cache, _config_cache_time
    now = time.time()
    if _config_cache is not None and (now - _config_cache_time) < _CONFIG_CACHE_TTL:
        return dict(_config_cache)

    path = _cache_config_path()
    cfg = dict(_DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
        except Exception as e:
            logger.debug("缓存配置加载失败，使用默认值: %s", e)

    _config_cache = cfg
    _config_cache_time = now
    return dict(cfg)


# ── 原子写 + 跨进程锁 ──────────────────────────────────────
# 实现已收口到共享模块 config_io：
#   atomic_write_json —— 唯一临时文件 + 写后校验 + os.replace 原子替换
#   config_write_lock —— msvcrt 跨进程排他锁（同目录 <name>.lock，常驻不删），
#                        **严格版**：拿不到锁抛 ConfigLockTimeout，事务体不执行
#                        ⇒ 绝不"未持锁还写入"（那会覆盖别人的更新 = 丢数据）


def save_cache_config(cfg: dict) -> bool:
    """保存缓存配置（唯一临时文件 + 跨进程严格锁 + 原子替换）。

    返回是否真的写入：
    - True  = 已落盘
    - False = 拿不到跨进程锁（本次**未写入**，原文件保持不变）或写失败

    失败不抛异常（避免 GUI 因配置写失败而崩），但**必须**让调用方能知道
    这次没存上 ⇒ 返回 False + 记 error 日志。
    """
    global _config_cache, _config_cache_time
    path = _cache_config_path()
    with _config_lock:  # 同进程线程锁：挡同进程多线程，与跨进程锁不重复
        try:
            with config_io.config_write_lock(path):
                _atomic_write_json(path, cfg)
        except config_io.ConfigLockTimeout as e:
            logger.error("缓存配置保存被跳过（拿不到跨进程锁，本次未写入）: %s（%s）", path, e)
            return False
        except Exception as e:
            logger.error("缓存配置保存失败: %s", e)
            return False
        # 更新缓存
        _config_cache = cfg
        _config_cache_time = time.time()
        return True


def _resolve_cache_root() -> str:
    """解析缓存根目录路径（**不创建**目录）

    ★ 与 _cache_root() 的区别：这里**只解析不建目录** ⇒
      纯查询接口（is_job_protected / protect_dir）调用它时
      不会在磁盘上留下任何痕迹。
    """
    cfg = load_cache_config()
    return cfg.get("cache_root", _default_cache_root())


def _cache_root() -> str:
    """获取缓存根目录（从配置读取，不存在则创建）"""
    root = _resolve_cache_root()
    os.makedirs(root, exist_ok=True)
    return root


def _is_inside(root: str, path: str) -> bool:
    """
    path 是否位于 root 之内（防 job_id 里的 ".." / 路径分隔符越界）。

    ★ 与 page_meta._is_inside **同款口径**（commonpath 比较），但这里是
      **本地等价实现**而不是 `from page_meta import _is_inside`：
      cache_manager 是最底层的缓存模块，让它反向依赖 page_meta（页元数据
      功能模块）会引入不必要的模块耦合与潜在的循环 import 风险；
      这段判断只有几行，就地实现更稳。
    """
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    try:
        return os.path.commonpath([root_abs, path_abs]) == root_abs
    except ValueError:
        return False   # 不同盘符


def _job_dir_path(job_id: str) -> str:
    """
    把 job_id 解析成缓存根内的目录路径（**不创建**目录）。

    ★ 越界防护（V1-N-5）：job_id 里含 ".." 或路径分隔符时，解析结果可能
      落在缓存根**之外** —— 那样会在根外建目录 / 删目录。
      ⇒ 这里统一做 _is_inside 校验，越界即**拒绝并抛错**，
        绝不默默在缓存根之外创建或删除任何东西。
      正常路径（内部时间戳 job_id）行为完全不变。
    """
    root = _cache_root()
    d = os.path.join(root, job_id)
    if not _is_inside(root, d):
        logger.error("拒绝越界的 job 目录访问: %r（缓存根: %s）", job_id, root)
        raise ValueError(f"job_id 越界缓存根: {job_id!r}")
    return d


def _job_dir(job_id: str) -> str:
    """获取指定 job 的缓存目录（不存在则创建）"""
    d = _job_dir_path(job_id)
    os.makedirs(d, exist_ok=True)
    return d


# ── 活跃任务管理 ──

def register_job(job_id: str):
    """注册活跃任务，保护其缓存不被 eviction 清理"""
    with _lock:
        _active_jobs.add(job_id)


def unregister_job(job_id: str):
    """取消注册活跃任务（不再受 eviction 保护）"""
    with _lock:
        _active_jobs.discard(job_id)


def register_pending_job(job_id: str):
    """
    登记为"已扫描未保存"任务 —— 受 eviction / stale 清理保护。

    调用时机：扫描**成功结束**、结果交给用户预览之前。
    注销时机（二选一，必须有）：
      - 落盘成功 → save_manager._finalize → remove_job（删目录 + 解除保护）
      - 用户放弃（关闭预览不保存）→ unregister_pending_job（只解除保护）
    """
    with _lock:
        _active_jobs.discard(job_id)
        _pending_jobs.add(job_id)


def unregister_pending_job(job_id: str):
    """
    放弃"已扫描未保存"任务 —— 解除保护，但**不删**缓存目录。

    供 GUI 在"用户关闭预览且不保存"时调用（GUI 侧接线见修复报告）。
    重复调用是幂等安全的。
    """
    with _lock:
        _pending_jobs.discard(job_id)


def _is_protected_locked(name: str, root: str | None = None) -> bool:
    """
    判断一个 job 目录名当前是否受保护（**调用者须持有 _lock**）。

    三个保护来源：
      - _active_jobs    正在扫描
      - _pending_jobs   已扫描未保存
      - _protected_dirs 显式保护（protect_dir）

    ★ root：显式保护**按缓存根分组**（见 _protected_dirs 的注释）⇒
      只有"在同一个缓存根下登记"的显式保护才生效。
      root 省略时用当前缓存根（纯查询路径）。
    """
    if name in _active_jobs or name in _pending_jobs:
        return True
    reg_root = _protected_dirs.get(name)
    if reg_root is None:
        return False
    if root is None:
        root = _resolve_cache_root()
    return os.path.normcase(os.path.abspath(reg_root)) == \
        os.path.normcase(os.path.abspath(root))


def _protected_names_locked(root: str | None = None) -> set[str]:
    """受保护 job 名的快照（**调用者须持有 _lock**）"""
    if root is None:
        root = _resolve_cache_root()
    root_key = os.path.normcase(os.path.abspath(root))
    explicit = {n for n, r in _protected_dirs.items()
                if os.path.normcase(os.path.abspath(r)) == root_key}
    return set(_active_jobs) | set(_pending_jobs) | explicit


def protect_dir(name: str) -> None:
    """
    登记一个 job 目录为**显式受保护**：eviction 与 cleanup_stale 都跳过它。

    用途：文档会话（document_session）跨进程存活，会话引用的扫描页留在原
    job 目录里 ⇒ 这些目录必须不被缓存清理误删（尤其是隔天续扫时
    cleanup_stale 的 24 小时过期线）。

    ★ 参数是**目录名**（job_id），与 register_job / register_pending_job
      的参数语义保持一致（不是完整路径）。
    ★ **幂等**：重复登记同一个名字不报错、不产生副作用。
    ★ **进程内语义**：_protected_dirs 是进程内状态，进程重启后即失效。
      本模块**不负责**跨进程持久化保护 —— 跨进程恢复由 document_session
      负责：它加载会话时会重新 protect_dir 那些 job 目录。
    ★ 只挡**自动清理**（eviction / cleanup_stale）；remove_job() 的显式
      删除始终优先。
    """
    with _lock:
        _protected_dirs[name] = _resolve_cache_root()


def unprotect_dir(name: str) -> None:
    """
    取消对某个 job 目录的显式保护（会话完成 / 放弃时调用）。

    ★ **幂等**：对没保护过的名字调用不报错、不产生副作用。
    ★ 只解除保护，**不删目录**（删不删由调用方决定）。
    """
    with _lock:
        _protected_dirs.pop(name, None)


def is_job_protected(job_id: str) -> bool:
    """该 job 当前是否受清理保护（正在扫描 或 已扫描未保存 或 显式保护）"""
    with _lock:
        return _is_protected_locked(job_id)


def protected_job_ids() -> set[str]:
    """当前受保护 job 的快照（调试 / 测试用）"""
    with _lock:
        return _protected_names_locked()


# ── 核心操作 ──

def write_page(job_id: str, page_num: int, data: bytes, ext: str) -> str:
    """
    将扫描页写入缓存。写入前检查是否需要清理。
    返回缓存文件路径。
    """
    # 写入前检查清理（而非写入后）
    with _lock:
        _maybe_evict_locked()

    d = _job_dir(job_id)
    path = os.path.join(d, f"page_{page_num:03d}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    # 增量修正缓存大小（E-3）：避免每次写页都全量 os.walk 缓存根
    _note_size_delta(len(data))
    logger.debug("缓存写入: %s (%d bytes)", path, len(data))

    return path


def read_page(job_id: str, page_num: int, ext: str) -> bytes:
    """从缓存读取指定页的数据"""
    d = _job_dir_path(job_id)
    path = os.path.join(d, f"page_{page_num:03d}.{ext}")
    with open(path, "rb") as f:
        return f.read()


def list_pages(job_id: str) -> list[tuple[int, str, str]]:
    """
    列出指定 job 的所有缓存页。
    返回 [(page_num, ext, path), ...] 按页码排序。
    """
    d = _job_dir_path(job_id)
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
    """删除指定 job 的全部缓存（同时解除活跃 / 待保存 / 显式保护）

    ★ **显式删除永远优先于保护**：本函数**不受** protect_dir / 显式保护
      的阻挡 —— 即使该 job 处于受保护状态，目录照删不误。
      保护只挡**自动清理**（_evict_oldest_jobs_locked / cleanup_stale）。
      ⇒ 本函数**不**因"受保护"而拒绝删除，既有语义不变。

    ★ 三种保护集合**一并解除**（与既有 active / pending 行为一致）：
      remove_job 是 job 的生命周期终点，目录都没了，"还受保护"是假状态
      ⇒ 保持既有不变量「remove_job 之后 is_job_protected 为 False」。

    ★ 越界防护（V1-N-5）：job_id 越出缓存根时**拒绝删除**并记 error
      （不抛异常 —— 本函数在 save_manager._finalize 的落盘主路径上，
      抛异常会把"保存成功"变成"保存失败"）；保护集合照常解除。
    """
    with _lock:
        try:
            d = _job_dir_path(job_id)
        except ValueError as e:
            logger.error("拒绝删除越界的 job 目录: %s（%s）", job_id, e)
            d = None
        if d is not None and os.path.isdir(d):
            removed = _dir_size_bytes(d)
            shutil.rmtree(d, ignore_errors=True)
            logger.debug("缓存清除: %s", d)
            _note_size_delta(-removed)
        _active_jobs.discard(job_id)
        _pending_jobs.discard(job_id)
        _protected_dirs.pop(job_id, None)


def _dir_size_bytes(d: str) -> int:
    """单个目录的总字节数（只遍历该目录，不扫缓存根）"""
    total = 0
    for dirpath, _, filenames in os.walk(d):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def _note_size_delta(delta: int) -> None:
    """按增量修正缓存大小（E-3：避免每次写/删都全量重算）"""
    global _cache_size_cache
    with _size_lock:
        if _cache_size_cache is not None:
            _cache_size_cache = (_cache_size_cache[0] + delta, _cache_size_cache[1])


def _cache_size_bytes() -> int:
    """
    计算当前缓存总大小（字节）。

    **增量计数 + 短 TTL 全量校准**（E-3）：
    write_page 每写一页都会触发一次清理检查，1GB 缓存下每次全量
    os.walk 缓存根 ⇒ 每页一次全盘遍历。
    现在：写页 / 删目录按增量修正计数（精确），只有 TTL 过期时才做一次
    全量校准（把外部进程改动等漏掉的偏差纠正回来）。
    """
    global _cache_size_cache
    with _size_lock:
        now = time.time()
        if (_cache_size_cache is not None
                and (now - _cache_size_cache[1]) < _CACHE_SIZE_TTL):
            return _cache_size_cache[0]

    root = _cache_root()
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    with _size_lock:
        _cache_size_cache = (total, time.time())
    return total


def _invalidate_cache_size():
    """缓存大小不可信时（外部改动）作废计数，下一次调用重新全量统计"""
    global _cache_size_cache
    with _size_lock:
        _cache_size_cache = None


def _cache_size_mb() -> float:
    """当前缓存大小（MB）"""
    return _cache_size_bytes() / (1024 * 1024)


# ── 清理逻辑（内部，调用者须持有 _lock）──

def _maybe_evict_locked():
    """检查缓存大小，接近触发线时执行 FIFO 清理。调用者须持有 _lock。"""
    cfg = load_cache_config()
    limit_bytes = cfg["cache_limit_mb"] * 1024 * 1024
    trigger_bytes = limit_bytes * cfg["trigger_ratio"]
    cleanup_bytes = cfg["cleanup_mb"] * 1024 * 1024

    current = _cache_size_bytes()
    if current < trigger_bytes:
        return

    logger.info("缓存 %.1fMB 超过触发线 %.1fMB，开始清理...",
                current / (1024 * 1024), trigger_bytes / (1024 * 1024))
    _evict_oldest_jobs_locked(cleanup_bytes)


def _evict_oldest_jobs_locked(target_bytes: int):
    """
    FIFO 清理：按任务目录创建时间从旧到新删除整个目录。
    受保护的任务（_active_jobs 正在扫描 + _pending_jobs 已扫描未保存
    + _protected_dirs 显式保护）不被清理。
    调用者须持有 _lock。
    """
    root = _cache_root()
    jobs = []

    for entry in os.listdir(root):
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        # 跳过受保护的任务（正在扫描 / 已扫描未保存 / 显式保护）
        if _is_protected_locked(entry, root):
            continue
        try:
            ctime = os.path.getctime(d)
            # 递归统计（V1-N-3）：删除用的是 shutil.rmtree（整棵子树），
            # 统计若只看目录**第一层**文件，freed 会被低估 ⇒ 增量计数偏高
            # ⇒ eviction 触发线偏早。用 _dir_size_bytes 与 remove_job /
            # cleanup_stale 保持同一口径。
            size = _dir_size_bytes(d)
            jobs.append((ctime, size, entry, d))
        except OSError:
            continue

    jobs.sort()  # 最旧的在前

    freed = 0
    for ctime, size, job_id, d in jobs:
        if freed >= target_bytes:
            break
        try:
            shutil.rmtree(d, ignore_errors=True)
            freed += size
            logger.debug("清理缓存任务: %s (%.1fMB)", job_id, size / (1024 * 1024))
        except OSError as e:
            logger.debug("清理失败: %s: %s", d, e)

    logger.info("缓存清理完成，释放 %.1fMB", freed / (1024 * 1024))
    if freed:
        _note_size_delta(-freed)


def cleanup_stale():
    """启动时清理超过 stale_hours 的残留缓存（防崩溃遗留）"""
    cfg = load_cache_config()
    stale_seconds = cfg["stale_hours"] * 3600
    root = _cache_root()
    now = time.time()
    # 快照受保护集合（持锁读，避免与并发登记/注销竞态）
    with _lock:
        protected = _protected_names_locked(root)

    for entry in os.listdir(root):
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        # 受保护的任务（正在扫描 / 已扫描未保存 / 显式保护）不参与过期清理
        if entry in protected:
            continue
        try:
            mtime = os.path.getmtime(d)
            if now - mtime > stale_seconds:
                removed = _dir_size_bytes(d)
                shutil.rmtree(d, ignore_errors=True)
                _note_size_delta(-removed)
                logger.debug("清理过期缓存: %s (age: %.1fh)", entry,
                             (now - mtime) / 3600)
        except OSError:
            pass
