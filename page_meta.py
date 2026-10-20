"""
页面元数据 — HP Scan Tool v4.0
记录每页的原始几何与处理参数，使纠偏/续扫/双面/大批量合并可回溯。

并发与清理约定（v4.0）：
  1) 写入复用共享模块 config_io（不再另写一套锁）：
     - atomic_write_json —— 唯一临时名 + 写后校验 + os.replace 原子替换
     - config_lock       —— msvcrt 跨进程排他锁（锁住整个"读-改-写"事务）
     另外加一把**进程内** threading.Lock：config_lock 拿不到锁时会记日志
     并放行写入（同进程多线程若只靠它，就可能真的丢更新），
     进程内锁保证同进程并发永远串行。
  2) 孤儿清理：缓存 job 目录已不存在的元数据条目会被删除（prune_orphans），
     一次性覆盖"扫描失败/取消、缓存被 eviction、程序异常退出"等所有
     "缓存没了但元数据还在"的路径，避免 page_meta.json 条目单调增长。
"""

import contextlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from typing import Optional

import config_io

logger = logging.getLogger(__name__)

# 进程内写事务锁：按元数据文件路径分桶，跨 PageMetaStore 实例共享
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _write_lock(path: str) -> threading.Lock:
    """取某个元数据文件的进程内写锁（首次调用时创建）"""
    key = os.path.normcase(os.path.abspath(path))
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def _is_inside(root: str, path: str) -> bool:
    """path 是否位于 root 之内（防 job_id 里的路径分隔符越界）"""
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    try:
        return os.path.commonpath([root_abs, path_abs]) == root_abs
    except ValueError:
        return False   # 不同盘符


@dataclass
class PageMeta:
    """单页元数据。cache_path 指向**永不修改**的原始扫描件。"""
    cache_path: str
    width: int
    height: int
    manual_rotation: int = 0        # 用户手动旋转 0/90/180/270
    deskew_angle: float = 0.0       # 自动纠偏角（度），批次 4 使用
    a4_normalized: bool = False     # 落盘时是否做过 A4 画布归一
    crop_rect: Optional[tuple] = None   # 自动裁边框


class PageMetaStore:
    """按 job 持久化页元数据（原子写 + 事务级锁：进程内线程锁 + 跨进程文件锁）"""

    def __init__(self, cache_root: str):
        self._cache_root = os.path.abspath(cache_root)
        self._path = os.path.join(self._cache_root, "page_meta.json")

    # ── 读 ──

    def load(self) -> dict:
        """读全部元数据（原子替换保证不会读到半截文件）"""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get_job(self, job_id: str) -> dict:
        return self.load().get(job_id, {})

    # ── 写 ──

    @contextlib.contextmanager
    def _transaction(self):
        """写事务临界区：进程内线程锁 → 跨进程文件锁（顺序固定，不会死锁）"""
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with _write_lock(self._path):
            with config_io.config_lock(self._path):
                yield

    def _write(self, metas: dict) -> None:
        """原子写（调用方须持有事务锁）"""
        config_io.atomic_write_json(self._path, metas)

    def save(self, metas: dict) -> None:
        """整体覆盖写（加锁 + 原子替换）"""
        with self._transaction():
            self._write(metas)

    def set_page(self, job_id: str, page_no: int, meta: PageMeta) -> None:
        """记录一页元数据（读-改-写全程持锁，并发调用不会互相覆盖）"""
        payload = asdict(meta)
        with self._transaction():
            metas = self.load()
            metas.setdefault(job_id, {})[str(page_no)] = payload
            self._write(metas)

    def remove_job(self, job_id: str) -> None:
        """
        删除某 job 的元数据，并在**同一事务内**顺带清理孤儿条目。

        孤儿 = 缓存 job 目录已不存在的条目（扫描失败/取消、缓存被 eviction、
        程序异常退出等路径留下的残留）。保存成功后的清理走这里。
        """
        with self._transaction():
            metas = self.load()
            changed = self._drop_job_locked(metas, job_id)
            changed = self._prune_locked(metas) or changed
            if changed:
                self._write(metas)

    # ── 孤儿清理 ──

    def prune_orphans(self, cache_root: str = None) -> list[str]:
        """
        清理孤儿条目：缓存 job 目录已不存在的元数据条目。

        返回被删除的 job_id 列表。
        """
        root = os.path.abspath(cache_root) if cache_root else self._cache_root
        if not os.path.isdir(root):
            # 缓存根不存在（换了根/盘没挂上）⇒ 不动元数据，避免误删全部
            logger.debug("缓存根不存在，跳过孤儿清理: %s", root)
            return []
        with self._transaction():
            metas = self.load()
            removed = self._prune_locked(metas, root)
            if removed:
                self._write(metas)
            return removed

    def _prune_locked(self, metas: dict, root: str = None) -> list[str]:
        """从 metas 中剔除孤儿条目（调用方须持有事务锁）"""
        root = os.path.abspath(root) if root else self._cache_root
        removed = []
        for job_id in list(metas.keys()):
            job_dir = os.path.join(root, job_id)
            if _is_inside(root, job_dir) and os.path.isdir(job_dir):
                continue      # 缓存目录还在 ⇒ 不是孤儿
            metas.pop(job_id, None)
            removed.append(job_id)
        if removed:
            logger.debug("清理孤儿页元数据: %s", removed)
        return removed

    def _drop_job_locked(self, metas: dict, job_id: str) -> bool:
        """删除 metas 中的某个 job（调用方须持有事务锁），返回是否发生变化"""
        sentinel = object()
        return metas.pop(job_id, sentinel) is not sentinel


def prune_orphans(cache_root: str) -> list[str]:
    """模块级入口：清理 cache_root 下已无缓存目录的孤儿页元数据"""
    return PageMetaStore(cache_root).prune_orphans()
