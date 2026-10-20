"""
页面元数据 — HP Scan Tool v4.0
记录每页的原始几何与处理参数，使纠偏/续扫/双面/大批量合并可回溯。

并发与清理约定（v4.0）：
  1) 写入复用共享模块 config_io（不再另写一套锁）：
     - atomic_write_json —— 唯一临时名 + 写后校验 + os.replace 原子替换
     - config_write_lock —— msvcrt 跨进程排他锁（锁住整个"读-改-写"事务），
       **拿不到锁就抛 ConfigLockTimeout**，事务体不执行 ⇒ 绝不"未持锁还写入"
       （旧配置的 2 秒等待上限在机器满载时会被正常触发，导致丢更新；
        config_io 的等待上限已提到 30 秒）
     另外加一把**进程内** threading.Lock：同进程多线程必须串行。
  2) 锁超时**不抛给调用方**（PySide6 ≥ 6.5 对槽内未捕获异常会 qFatal ⇒ 进程 abort）：
     记 error 日志 + 跳过本次写入，并用返回值（False）明确告诉调用方
     "这次写入没有发生"。
  3) 元数据文件损坏时：记 error 日志 + **备份原文**（与 history_manager 同风格），
     并且**禁止**用空字典覆盖原文件（否则其它 job 的页元数据被永久抹掉）。
  4) 孤儿清理：缓存 job 目录已不存在的元数据条目会被删除（prune_orphans），
     一次性覆盖"扫描失败/取消、缓存被 eviction、程序异常退出"等所有
     "缓存没了但元数据还在"的路径，避免 page_meta.json 条目单调增长。
     ⇒ 与 cache_manager 的 job 生命周期一致：**job 目录被删 ⇒ 元数据也删**；
       还在缓存目录里的 job（还在用）**绝不**被当成孤儿删掉。
  5) 进程内容器**有界**（不引后台线程、不引定时器 ⇒ CLI 也能用）：
     - _WRITE_LOCKS：只在**写事务进行中**保留条目，用完立刻回收
       （回收时"还有人在用/在等"的锁绝不会被删 —— 删了就会两把锁交错
        读-改-写 ⇒ 丢更新）；
     - _CORRUPT_BACKED_UP：FIFO 淘汰，上限 64 条
       （"同一份损坏内容只备份一次"的去重语义不变）。
     ⇒ 长跑进程（扫几千次 / 换缓存根 / 反复遇到损坏文件）不会只涨内存。
"""

import collections
import contextlib
import json
import logging
import os
import shutil
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

import config_io

logger = logging.getLogger(__name__)

# 进程内写事务锁：按元数据文件路径分桶，跨 PageMetaStore 实例共享。
# ★ 每个条目是 [lock, 在用计数]：
#   - 计数 > 0 ⇒ 有人正在用（或正在排队等）这把锁 ⇒ **绝不能**回收；
#   - 计数归零 ⇒ 没人再用了 ⇒ 立刻删掉 ⇒ 换缓存根 / 换临时目录不会
#     在这张表里永久堆积锁对象（终审 F-13：以前只增不减）。
_WRITE_LOCKS: dict[str, list] = {}
_WRITE_LOCKS_GUARD = threading.Lock()

# 已备份过的损坏文件（路径 + mtime + size），避免反复读取刷出一堆备份
# ★ 上限 + FIFO 淘汰：这个集合以前只增不减（每次"损坏内容变了"就多一个 key），
#   长跑进程会一直涨（终审 F-13）。
_CORRUPT_BACKED_UP: "collections.OrderedDict[tuple, None]" = collections.OrderedDict()
_CORRUPT_BACKED_UP_MAX = 64


@contextlib.contextmanager
def _write_lock(path: str):
    """
    取某个元数据文件的进程内写锁（首次调用时创建，**用完即回收**）。

    ★ 做成上下文管理器（而不是"返回锁对象"）：只有这样才能在释放之后
      把"没人再用的锁"从表里删掉，同时保证"还有人在用 / 在等"的锁
      不会被回收 —— 回收了就等于给下一个调用方发了一把**新**锁，
      两把锁可以交错读-改-写 ⇒ 丢更新。
    """
    key = os.path.normcase(os.path.abspath(path))
    with _WRITE_LOCKS_GUARD:
        entry = _WRITE_LOCKS.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _WRITE_LOCKS[key] = entry
        entry[1] += 1
        lock = entry[0]
    try:
        with lock:
            yield
    finally:
        with _WRITE_LOCKS_GUARD:
            entry[1] -= 1
            if entry[1] <= 0:
                _WRITE_LOCKS.pop(key, None)


def _is_inside(root: str, path: str) -> bool:
    """path 是否位于 root 之内（防 job_id 里的路径分隔符越界）"""
    root_abs = os.path.abspath(root)
    path_abs = os.path.abspath(path)
    try:
        return os.path.commonpath([root_abs, path_abs]) == root_abs
    except ValueError:
        return False   # 不同盘符


def _backup_corrupt(fpath: str):
    """
    把损坏的页元数据文件原文备份成 <name>.corrupt-<时间戳>，返回备份路径。

    与 history_manager._backup_corrupt 同一套命名与去重规则：
    同一份损坏内容只备份一次（路径+mtime+size 去重），备份失败不影响主流程
    （原文仍在原处，不会被静默清空）。
    """
    try:
        st = os.stat(fpath)
    except OSError:
        return None
    key = (fpath, st.st_mtime_ns, st.st_size)
    if key in _CORRUPT_BACKED_UP:
        return None
    # ★ 登记并**保持有界**：FIFO 淘汰最旧的（同一份损坏内容只备份一次的
    #   去重语义不变，只是"很久以前"的损坏版本不再占位）。
    _CORRUPT_BACKED_UP[key] = None
    while len(_CORRUPT_BACKED_UP) > _CORRUPT_BACKED_UP_MAX:
        _CORRUPT_BACKED_UP.popitem(last=False)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = f"{fpath}.corrupt-{ts}"
    try:
        shutil.copyfile(fpath, backup)
    except OSError as e:
        logger.error("页元数据损坏备份失败: %s（%s）", fpath, e)
        return None
    logger.error("页元数据文件已损坏，原文已备份到: %s", backup)
    return backup


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
        data, _ = self._load_state()
        return data

    def _load_state(self) -> tuple[dict, bool]:
        """
        读全部元数据，返回 (数据, 是否**可以安全覆盖**原文件)。

        - 文件不存在 → ({}, True)：正常首次写入
        - 文件是空的 → ({}, True)：没有任何数据可丢，视为可安全覆盖
        - 文件损坏（JSON 解析失败 / 编码乱码 / 顶层不是对象）→ 记 error 日志 +
          **备份原文**，返回 ({}, False)：调用方**禁止**用空字典覆盖原文件，
          否则其它 job 的页元数据（手动旋转、裁剪框）会被永久抹掉。
        """
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return {}, True
        except UnicodeDecodeError as e:
            logger.error("页元数据文件编码损坏: %s（%s）", self._path, e)
            _backup_corrupt(self._path)
            return {}, False
        except OSError as e:
            logger.error("页元数据文件读取失败: %s（%s）", self._path, e)
            return {}, False
        if not raw.strip():
            # 空文件：没有任何数据可丢，视为可安全覆盖
            return {}, True
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("页元数据文件损坏: %s（%s）", self._path, e)
            _backup_corrupt(self._path)
            return {}, False
        if not isinstance(data, dict):
            logger.error("页元数据文件格式错误（期望对象）: %s", self._path)
            _backup_corrupt(self._path)
            return {}, False
        return data, True

    def get_job(self, job_id: str) -> dict:
        return self.load().get(job_id, {})

    # ── 写 ──

    @contextlib.contextmanager
    def _transaction(self):
        """
        写事务临界区：进程内线程锁 → 跨进程文件锁（顺序固定，不会死锁）。

        yield True  = 拿到锁，事务体可以写
        yield False = 没拿到锁（等待超时），事务体**不得写**——本次写入已跳过

        ★ 锁超时不向上抛：page_meta 会被 GUI 与工作线程调用，
          PySide6 ≥ 6.5 对槽内未捕获异常会 qFatal ⇒ 进程 abort。
        """
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with _write_lock(self._path):
            with contextlib.ExitStack() as stack:
                try:
                    stack.enter_context(config_io.config_write_lock(self._path))
                except config_io.ConfigLockTimeout as e:
                    logger.error("页元数据写入被跳过（拿不到跨进程锁）: %s（%s）",
                                 self._path, e)
                    yield False
                    return
                yield True

    def _write(self, metas: dict) -> None:
        """原子写（调用方须持有事务锁）"""
        config_io.atomic_write_json(self._path, metas)

    def save(self, metas: dict) -> bool:
        """整体覆盖写（加锁 + 原子替换）。返回是否真的写入。"""
        with self._transaction() as acquired:
            if not acquired:
                return False
            self._write(metas)
            return True

    def set_page(self, job_id: str, page_no: int, meta: PageMeta) -> bool:
        """
        记录一页元数据（读-改-写全程持锁，并发调用不会互相覆盖）。

        返回 False 表示本次写入**没有发生**：拿不到跨进程锁，或原文件已损坏
        （损坏时只备份不覆盖，绝不抹掉其它 job 的元数据）。
        """
        payload = asdict(meta)
        with self._transaction() as acquired:
            if not acquired:
                return False
            metas, overwritable = self._load_state()
            if not overwritable:
                logger.error("页元数据原文件损坏，已跳过写入（原文与备份均保留）: %s",
                             self._path)
                return False
            metas.setdefault(job_id, {})[str(page_no)] = payload
            self._write(metas)
            return True

    def remove_job(self, job_id: str) -> bool:
        """
        删除某 job 的元数据，并在**同一事务内**顺带清理孤儿条目。

        孤儿 = 缓存 job 目录已不存在的条目（扫描失败/取消、缓存被 eviction、
        程序异常退出等路径留下的残留）。保存成功后的清理走这里。

        返回是否真的写入（False = 拿不到锁或原文件损坏，本次未改动）。
        """
        with self._transaction() as acquired:
            if not acquired:
                return False
            metas, overwritable = self._load_state()
            if not overwritable:
                logger.error("页元数据原文件损坏，已跳过删除（原文与备份均保留）: %s",
                             self._path)
                return False
            changed = self._drop_job_locked(metas, job_id)
            changed = self._prune_locked(metas) or changed
            if changed:
                self._write(metas)
            return True

    # ── 孤儿清理 ──

    def prune_orphans(self, cache_root: str = None) -> list[str]:
        """
        清理孤儿条目：缓存 job 目录已不存在的元数据条目。

        返回被删除的 job_id 列表（拿不到锁 / 原文件损坏时返回空列表，
        并已记 error 日志）。
        """
        root = os.path.abspath(cache_root) if cache_root else self._cache_root
        if not os.path.isdir(root):
            # 缓存根不存在（换了根/盘没挂上）⇒ 不动元数据，避免误删全部
            logger.debug("缓存根不存在，跳过孤儿清理: %s", root)
            return []
        with self._transaction() as acquired:
            if not acquired:
                return []
            metas, overwritable = self._load_state()
            if not overwritable:
                logger.error("页元数据原文件损坏，已跳过孤儿清理（原文与备份均保留）: %s",
                             self._path)
                return []
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
