"""
文档会话模型 — HP Scan Tool v4.0（批次 4 · B4-1）

把**多次扫描聚合成一个文档**：会话 = 若干批次（job）的页引用 + 一个状态机。

它是批次 4 三项新需求的共同地基：
  - A 手动双面：正面一批 + 背面一批 ⇒ 交错合并
  - B 断点续扫：会话持久化，进程重启后可继续
  - C 大批量合并：N 批次累积 ⇒ 合并成单个 PDF

设计要点（为什么这么写）
------------------------------------------------------------------------------
1) **页不复制**：会话只存**引用**（cache_path / job_id / page_no），
   页仍然留在原 job 目录 `cache_root/<job_id>/page_NNN.ext`。
   ⇒ 另建 session 目录会导致 page_meta 的孤儿清理（按 job 目录是否存在判孤儿）
     把会话页当孤儿删掉，而且多一次文件复制（1GB 文档 ⇒ 复制 1GB）。
2) **保护而非搬运**：会话引用了哪些 job，就保护哪些 job 目录，
   使其不被 eviction / cleanup_stale 清掉 ⇒ 跨天续扫时页路径不悬空。
   ★ `cache_manager.protect_dir` / `unprotect_dir` **早已落地**
     （cache_manager.py:305 / :326）⇒ 这里**直接调用**。
     早年的"函数不存在就静默跳过"写法已删除：那个函数一直都在，
     留着只会在保护真的失效时悄悄吞掉问题（终审 M-4）。
     保护失败只记 error 日志，不抛给调用方。
3) **读失败不清空**：JSON 解析失败 ⇒ 记 error 日志 + 返回 None，
   **原文件保留**（另存一份 `.corrupt-<时间戳>` 备份），与 history_manager /
   page_meta 的既有约定一致。绝不"用空会话覆盖原文件"。
4) **零新依赖 + 纯 Python**：只用标准库 + 本仓库已有模块（config_io /
   cache_manager），**不 import 任何 Qt**（CLI 要能复用这个模块）。

模块边界
------------------------------------------------------------------------------
本模块只做**模型 + 持久化**：
  - 合并页序（双面交错 / 批次拼接）⇒ B4-3 的 `doc_merge`
  - 落盘成 PDF / 图片 ⇒ B4-5 的 `save_manager`
  - GUI 接线 ⇒ B4-6
"""

import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

import cache_manager
import config_io

logger = logging.getLogger(__name__)

__all__ = [
    "DocumentSession",
    "InvalidTransitionError",
    "PageRef",
    "SessionStore",
    "can_transition",
    "new_session",
    "new_session_id",
    "sessions_dir",
]

# ── 常量 ────────────────────────────────────────────────────────────────

#: 会话模式
MODES = ("single", "duplex", "batch")

#: 会话状态
STATES = ("scanning", "awaiting_flip", "paused", "completed", "abandoned")

#: 活跃状态（会话还没结束 ⇒ 引用的 job 目录必须受保护）
ACTIVE_STATES = ("scanning", "awaiting_flip", "paused")

#: 终态（会话结束了 ⇒ 引用过的 job 目录可以解除保护）
TERMINAL_STATES = ("completed", "abandoned")

#: 页面来源（双面时 front/back 来自不同的 job）
SIDES = ("single", "front", "back")

#: 会话文件格式版本
SESSION_FILE_VERSION = 1

# ── 状态机 ──────────────────────────────────────────────────────────────
#
# 合法转换（**不在表内的转换一律抛错**，包括同态重复设置）：
#
#   scanning      ──> awaiting_flip     duplex 扫完正面
#   awaiting_flip ──> scanning          用户翻面后再次扫描
#   scanning      ──> paused            batch 扫完一批
#   paused        ──> scanning          继续追加批次
#   scanning / paused / awaiting_flip ──> completed   完成 / 保存 / 放弃暂存正面
#   任意状态      ──> abandoned         用户放弃
#
# 单面模式只是上面那张表的一个退化用法：scanning ──> completed。
_LEGAL_TRANSITIONS: set[tuple[str, str]] = {
    ("scanning", "awaiting_flip"),
    ("awaiting_flip", "scanning"),
    ("scanning", "paused"),
    ("paused", "scanning"),
    ("scanning", "completed"),
    ("paused", "completed"),
    ("awaiting_flip", "completed"),
}
# 任意状态 → abandoned（abandoned 本身不再往外走）
for _state in STATES:
    if _state != "abandoned":
        _LEGAL_TRANSITIONS.add((_state, "abandoned"))

# 进程内写事务锁：按会话文件路径分桶，跨 SessionStore 实例共享
# （跨进程由 config_io.config_write_lock 保证）
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()

# 已备份过的损坏文件（路径 + mtime + size），避免反复读取刷出一堆备份
_CORRUPT_BACKED_UP: set[tuple[str, int, int]] = set()

#: 损坏备份名撞名时的最大重试次数（同名 ⇒ 自动加序号 -2 / -3 …）
_BACKUP_NAME_MAX_RETRY = 999


class InvalidTransitionError(ValueError):
    """
    非法的状态转换。

    继承 ValueError：调用方既可以精确捕获本异常，也可以按 ValueError 兜底。
    """


def can_transition(from_state: str, to_state: str) -> bool:
    """该状态转换是否合法（供 GUI 决定按钮是否可点）"""
    return (from_state, to_state) in _LEGAL_TRANSITIONS


# ── 数据结构 ────────────────────────────────────────────────────────────


@dataclass
class PageRef:
    """会话里对一页缓存件的引用。**不复制文件**，只记录位置与处理参数。"""

    cache_path: str          # 该页在缓存里的绝对路径
    job_id: str              # 来源 job（双面时 front/back 来自不同 job）
    page_no: int             # 来源 job 内的页号
    side: str = "single"     # "single" | "front" | "back"
    manual_rotation: int = 0     # 用户手动旋转 0/90/180/270
    deskew_angle: float = 0.0    # 自动纠偏角（度）
    force_keep: bool = False    # 用户选了「强制保存」⇒ 落盘不纠偏

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PageRef":
        if not isinstance(data, dict):
            raise ValueError("页引用必须是对象")
        side = data.get("side", "single")
        if side not in SIDES:
            raise ValueError(f"未知的页面来源: {side!r}")
        return cls(
            cache_path=str(data.get("cache_path", "")),
            job_id=str(data.get("job_id", "")),
            page_no=int(data.get("page_no", 0) or 0),
            side=side,
            manual_rotation=int(data.get("manual_rotation", 0) or 0),
            deskew_angle=float(data.get("deskew_angle", 0.0) or 0.0),
            force_keep=bool(data.get("force_keep", False)),
        )


@dataclass
class DocumentSession:
    """一个文档会话：多次扫描聚合成一个文档。"""

    session_id: str
    mode: str = "single"              # "single" | "duplex" | "batch"
    state: str = "scanning"           # 见 STATES
    output_path: str = ""             # 最终产物路径（可空，用户保存时再定）
    ext: str = "pdf"                  # jpg | png | pdf
    dpi: int = 300
    exposure: Optional[dict] = None
    reverse_back: bool = True         # 双面：背面是否反转（默认反转）
    pages: list = field(default_factory=list)   # list[PageRef]
    #: 页面列表是否"以本对象为准"（= 发生过删页）。
    #: SessionStore.save() 默认 merge=True，若不照顾删页意图会把磁盘上多出来的页
    #: **复活** —— 一旦本会话删过页，落盘时必须**逐页尊重**这次删除。
    #: 标志由 remove_pages_from 置位（并随 payload 持久化，
    #: 所以"落盘 → 删页 → 再落盘""重启后删页"两条路都成立）。
    full_rewrite: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0

    #: 本副本的**基线快照**（**不写进文件**，随对象存活）：
    #: 本副本"读到"的那份磁盘内容（页内容 + 标量）。
    #:
    #: 用途：落盘时做**三方合并**（base=基线 / mine=本副本 / theirs=磁盘当前），
    #: 从而回答"磁盘相对**我**的基线改了什么" —— 这是判断"谁是陈旧副本"的
    #: **唯一可靠依据**。
    #:
    #: ★ 为什么不能用 updated_at 时间戳：
    #:   并发写时 updated_at 反映的是"谁先调用 add_pages"，**与内容谁更新无关**；
    #:   且 Windows 上 time.time() 的分辨率约 15.6ms，比两个写者的间隔还粗
    #:   ⇒ "比大小"的结论等同于掷骰子，后写者的整批数据会被静默丢弃。
    #:
    #: None = 不知道基线（手工构造的对象）⇒ 退化为"本副本优先"的旧行为。
    base_snapshot: Optional[dict] = field(default=None, repr=False, compare=False)

    # ── 便捷属性 ──

    @property
    def front_pages(self) -> list:
        """正面页（双面模式）"""
        return [p for p in self.pages if p.side == "front"]

    @property
    def back_pages(self) -> list:
        """背面页（双面模式）"""
        return [p for p in self.pages if p.side == "back"]

    @property
    def single_pages(self) -> list:
        """单面页"""
        return [p for p in self.pages if p.side == "single"]

    @property
    def referenced_job_ids(self) -> list:
        """会话引用过的 job_id（**去重**，按首次出现顺序）"""
        seen: set = set()
        result: list = []
        for p in self.pages:
            job_id = getattr(p, "job_id", None)
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            result.append(job_id)
        return result

    @property
    def total_pages(self) -> int:
        """会话累计页数"""
        return len(self.pages)

    @property
    def is_open(self) -> bool:
        """会话是否仍未结束（未 completed / 未 abandoned）"""
        return self.state not in TERMINAL_STATES

    # ── 便捷方法 ──

    def add_pages(self, job_id: str, page_paths, side: str = "single",
                  page_nos: Optional[list] = None) -> list:
        """
        追加一批页（一个 job 的一次扫描结果）。

        - `page_nos` 缺省时按该 job 内已有页号续编（从 1 开始）；
          调用方已知真实页号时应显式传入（缓存文件名是 page_NNN.ext）。
        - ★ **重复的合并键会被自动改号**（终审 Important I-1）：
          合并键是 `(job_id, page_no, side)`；若两页键相同，落盘时的三方合并
          要把页列表建成 `dict` ⇒ **后者覆盖前者** ⇒ **静默丢页、没有任何报错**
          （终审实测：3 页 → 2 页）。
          ⇒ 这里在**入口**就把重复键改掉：不抛异常（GUI 槽里抛异常会打断流程）、
            不丢页、并记 warning。
        - 返回新建的 PageRef 列表。
        """
        if side not in SIDES:
            raise ValueError(f"未知的页面来源: {side!r}")
        paths = list(page_paths or [])
        if page_nos is not None:
            page_nos = list(page_nos)
            if len(page_nos) != len(paths):
                raise ValueError("page_nos 长度必须与 page_paths 一致")
        next_no = self._next_page_no(job_id)
        used = {(p.job_id, p.page_no, p.side) for p in self.pages}
        created = []
        for i, path in enumerate(paths):
            page_no = int(page_nos[i]) if page_nos is not None else next_no
            key = (str(job_id), page_no, side)
            if key in used:
                # ★ 合并键重复 ⇒ 落盘时会**静默丢页** ⇒ 这里改成不冲突的页号
                page_no = self._next_free_page_no(job_id, side, used)
                logger.warning(
                    "页号重复：%s 的第 %d 页（%s）已存在 ⇒ 改用第 %d 页，"
                    "避免落盘时静默丢页", job_id, key[1], side, page_no)
            used.add((str(job_id), page_no, side))
            created.append(PageRef(
                cache_path=str(path),
                job_id=str(job_id),
                page_no=page_no,
                side=side,
            ))
            next_no = max(next_no, page_no) + 1
        self.pages.extend(created)
        self.updated_at = time.time()
        return created

    def _next_free_page_no(self, job_id: str, side: str, used: set) -> int:
        """该 job 该 side 内第一个**未被占用**的页号（合并键不得重复）。"""
        n = self._next_page_no(job_id)
        while (str(job_id), n, side) in used:
            n += 1
        return n

    def remove_pages_from(self, job_id: str, page_no: int,
                          drop_cache: bool = True) -> list:
        """
        丢弃某 job 内从 `page_no` 起的所有页 —— "从第 N 页重扫"用。

        - 范围限定在**该 job 内**：会话里其它 job（先前批次）的页**不受影响**
        - `drop_cache=True` 时同时删除对应的缓存页文件
        - 被丢页的**页元数据不主动删**：缓存目录没了之后 `page_meta.prune_orphans`
          会把它当孤儿清掉（既有机制，不在这里重复实现）
        - 返回被移除的 `PageRef` 列表（保持原顺序）
        """
        jid = str(job_id)
        cut = int(page_no)
        dropped = [p for p in self.pages
                   if p.job_id == jid and p.page_no >= cut]
        if not dropped:
            return []
        self.pages = [p for p in self.pages
                      if not (p.job_id == jid and p.page_no >= cut)]
        # ★ 这次删除必须能活过落盘：SessionStore.save() 默认 merge=True，
        #   若不照顾删页意图会把磁盘上多出来的页**复活** ⇒ 置位 full_rewrite，
        #   save() 落盘时按"逐页三方合并"尊重这次删除（我删的页不留在磁盘上）。
        self.full_rewrite = True
        self.updated_at = time.time()
        if drop_cache:
            for p in dropped:
                try:
                    if p.cache_path and os.path.isfile(p.cache_path):
                        os.remove(p.cache_path)
                except OSError as e:
                    logger.debug("删除被丢弃的缓存页失败 [%s]: %s", p.cache_path, e)
        return dropped

    def _next_page_no(self, job_id: str) -> int:
        """该 job 内下一个可用页号"""
        nos = [p.page_no for p in self.pages if p.job_id == job_id]
        return (max(nos) + 1) if nos else 1

    # ── 状态机 ──

    def can_transition(self, to_state: str) -> bool:
        """从当前状态转到 to_state 是否合法"""
        return can_transition(self.state, to_state)

    def set_state(self, new_state: str) -> str:
        """
        切换状态。**非法转换抛 InvalidTransitionError**（不静默接受）。

        注意：同态重复设置（如 scanning → scanning）**不在合法转换表内**，
        同样抛错 —— 调用方应先判断当前状态再决定要不要切换。
        """
        if new_state not in STATES:
            raise ValueError(f"未知的会话状态: {new_state!r}")
        if not self.can_transition(new_state):
            raise InvalidTransitionError(
                f"非法的状态转换: {self.state} → {new_state}"
                f"（会话 {self.session_id}，模式 {self.mode}）")
        self.state = new_state
        self.updated_at = time.time()
        return self.state

    # 状态机便捷入口（语义比 set_state 清楚，行为完全一致）
    def resume_scanning(self) -> str:
        return self.set_state("scanning")

    def mark_awaiting_flip(self) -> str:
        return self.set_state("awaiting_flip")

    def mark_paused(self) -> str:
        return self.set_state("paused")

    def mark_completed(self) -> str:
        return self.set_state("completed")

    def mark_abandoned(self) -> str:
        return self.set_state("abandoned")

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "version": SESSION_FILE_VERSION,
            "session_id": self.session_id,
            "mode": self.mode,
            "state": self.state,
            "output_path": self.output_path,
            "ext": self.ext,
            "dpi": self.dpi,
            "exposure": self.exposure,
            "reverse_back": self.reverse_back,
            "pages": [p.to_dict() for p in self.pages],
            "full_rewrite": self.full_rewrite,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentSession":
        if not isinstance(data, dict):
            raise ValueError("会话文件必须是对象")
        mode = data.get("mode")
        state = data.get("state")
        if mode not in MODES:
            raise ValueError(f"未知的会话模式: {mode!r}")
        if state not in STATES:
            raise ValueError(f"未知的会话状态: {state!r}")
        exposure = data.get("exposure")
        if exposure is not None and not isinstance(exposure, dict):
            raise ValueError("exposure 必须是对象或 null")
        pages = [PageRef.from_dict(p) for p in (data.get("pages") or [])]
        session = cls(
            session_id=str(data.get("session_id", "")),
            mode=mode,
            state=state,
            output_path=str(data.get("output_path", "")),
            ext=str(data.get("ext", "pdf")),
            dpi=int(data.get("dpi", 300) or 300),
            exposure=exposure,
            reverse_back=bool(data.get("reverse_back", True)),
            pages=pages,
            full_rewrite=bool(data.get("full_rewrite", False)),
            created_at=float(data.get("created_at", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )
        # ★ 基线 = 刚读到的这份磁盘内容 ⇒ 之后落盘时能分辨
        #   "磁盘相对我改了啥" 与 "我相对磁盘改了啥"（三方合并的 base）。
        session.base_snapshot = _snapshot_of(data)
        return session


# ── 会话工厂 ────────────────────────────────────────────────────────────


def new_session_id() -> str:
    """生成一个可安全用作文件名的会话号"""
    return f"sess_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def new_session(mode: str = "single", output_path: str = "", ext: str = "pdf",
                dpi: int = 300, exposure: Optional[dict] = None,
                reverse_back: bool = True, session_id: Optional[str] = None) -> DocumentSession:
    """新建一个会话（状态 = scanning）"""
    if mode not in MODES:
        raise ValueError(f"未知的会话模式: {mode!r}")
    now = time.time()
    session = DocumentSession(
        session_id=session_id or new_session_id(),
        mode=mode,
        state="scanning",
        output_path=output_path,
        ext=ext,
        dpi=dpi,
        exposure=exposure,
        reverse_back=reverse_back,
        pages=[],
        created_at=now,
        updated_at=now,
    )
    # ★ 新会话的基线 = "磁盘上还没有这个会话"（页为空、标量为默认值）
    session.base_snapshot = _snapshot_of(session.to_dict())
    return session


def sessions_dir() -> str:
    """
    会话目录：%APPDATA%\\HP_Scan_Tool\\sessions\\

    **必须**通过 `config_io.config_dir()` 派生（不自己拼 %APPDATA%），
    这样测试只要 monkeypatch `config_io.config_dir` 就能整体重定向。
    """
    base = config_io.config_dir()
    path = os.path.join(base, "sessions")
    os.makedirs(path, exist_ok=True)
    return path


# ── 内部工具 ────────────────────────────────────────────────────────────


def _write_lock(path: str) -> threading.Lock:
    """取某个会话文件的进程内写锁（首次调用时创建）"""
    key = os.path.normcase(os.path.abspath(path))
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def _validate_session_id(session_id: str) -> str:
    """校验 session_id 可安全用作文件名（挡路径分隔符 / 越界）"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id 不能为空")
    if any(c in sid for c in '/\\:*?"<>|') or ".." in sid:
        raise ValueError(f"非法的 session_id: {session_id!r}")
    return sid


def _remove_quietly(path: str) -> None:
    """删文件，失败不抛（备份失败时清理残留的占位空文件）"""
    try:
        os.remove(path)
    except OSError:
        pass


def _reserve_backup_path(base_path: str) -> Optional[str]:
    """
    预留一个**唯一**的损坏备份路径（独占创建），返回该路径；文件已建好、内容待写入。

    ★ 为什么不能只靠时间戳：Windows 系统时钟分辨率只有 **15.625ms**（实测
      `time.get_clock_info('time').resolution == 0.015625`）⇒ 相邻两次
      `datetime.now().strftime('%f')` 有 99.5% 概率**完全相同** ⇒ 两次损坏
      备份算出**同一个文件名** ⇒ 第二次**覆盖**第一次 ⇒ **证据丢失**。

    ★ 这里的分工：**时间戳只负责可读性**（人一眼看出大概何时损坏），
      **唯一性由「独占创建（O_CREAT|O_EXCL）+ 同名自动加序号」保证**
      ⇒ 与时钟分辨率**完全无关**。
    首个备份名仍是 `<name>.corrupt-<YYYYMMDD-HHMMSS-ffffff>`（既有断言不变），
    真撞名时追加 `-2` / `-3` …（同前缀、同时间戳，人工仍看得出是同一批备份）。

    返回 None = 所有候选名都被占用 / 创建失败（此时不写任何内容）。
    """
    for idx in range(_BACKUP_NAME_MAX_RETRY + 1):
        candidate = base_path if idx == 0 else "%s-%d" % (base_path, idx + 1)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue          # 名字已被占用 ⇒ 换下一个序号
        except OSError as e:
            logger.error("会话损坏备份文件创建失败: %s（%s）", candidate, e)
            return None
        os.close(fd)
        return candidate
    logger.error("会话损坏备份文件名候选全部被占用: %s", base_path)
    return None


def _backup_corrupt(path: str) -> Optional[str]:
    """
    把损坏的会话文件原文备份成 <name>.corrupt-<时间戳>，返回备份路径。

    - 时间戳只负责**可读性**；备份名**唯一性**由「独占创建 + 同名加序号」保证
      —— Windows 时钟分辨率只有 15.6ms，两次损坏很容易算出同一个时间戳
      （旧实现会让后一次**覆盖**前一次 ⇒ 证据丢失）
    - 同一份损坏内容只备份一次（路径+mtime+size 去重），备份失败不影响主流程
      （原文仍在原处，不会被静默清空）
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    if key in _CORRUPT_BACKED_UP:
        return None
    _CORRUPT_BACKED_UP.add(key)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = _reserve_backup_path("%s.corrupt-%s" % (path, ts))
    if backup is None:
        return None
    try:
        shutil.copyfile(path, backup)
    except OSError as e:
        logger.error("会话文件损坏备份失败: %s（%s）", path, e)
        _remove_quietly(backup)
        return None
    logger.error("会话文件已损坏，原文已备份到: %s", backup)
    return backup


#: 参与三方合并的标量字段
#: （pages / created_at / updated_at / full_rewrite 另有专门规则，不在此列）
_MERGE_FIELDS = ("mode", "state", "output_path", "ext", "dpi",
                 "exposure", "reverse_back")


def _page_key(page: dict) -> tuple:
    """页的合并键：(job_id, page_no, side)"""
    return (page.get("job_id"), page.get("page_no"), page.get("side"))


def _snapshot_of(payload: dict) -> dict:
    """
    把一份会话 payload 变成**基线快照**（页内容 + 标量）。

    基线 = "本副本读到的那份磁盘内容" ⇒ 落盘时可以做三方合并：
    base=基线、mine=本副本、theirs=磁盘当前。

    ★ 为什么不拿 updated_at 当基线：并发写下它只反映"谁先调用 add_pages"，
      与"内容谁更新"无关（详见 DocumentSession.base_snapshot 的说明）。
    """
    pages: dict = {}
    for p in (payload.get("pages") or []):
        pages[_page_key(p)] = dict(p)
    return {
        "pages": pages,
        "fields": {f: payload.get(f) for f in _MERGE_FIELDS},
    }


def _merge_payload(base: Optional[dict], mine: dict, disk: dict) -> dict:
    """
    **三方合并**：base=本副本的基线快照，mine=本副本内容，disk=磁盘当前内容。

    ── 为什么不用"updated_at 比大小"（旧实现）────────────────────────
    旧实现：`if mine.updated_at < disk.updated_at: return`（整批放弃写入）。
      ① 并发写下 updated_at 反映的是"**谁先调用 add_pages**"，与"内容谁更新"
         无关 ⇒ 后落盘的副本可能整批数据被静默丢弃（用户侧："这批页不见了"）；
      ② Windows 上 time.time() 分辨率约 15.6ms，比两个写者的间隔还粗
         ⇒ 比较结论等同于掷骰子（实测 40 轮里红 7 次）；
      ③ 时间戳表达不了"磁盘删过页" ⇒ 陈旧副本会把删掉的页**复活**。

    ── 页的判定（逐键三方）──────────────────────────────────────────
      ① base 有、我有、磁盘没有 ⇒ **磁盘删过它** ⇒ 不复活（丢弃我的那份）
      ② base 有、我没有、磁盘还有 ⇒ **我删过它** ⇒ 删除优先（丢弃磁盘那份）
      ③ 其余 ⇒ 并发追加 ⇒ **两边都保留**
         同一页的内容：相对 base 我没改过 ⇒ 用磁盘的（别把磁盘上更新的
         纠偏角/手动旋转改回去）；我改过 ⇒ 用我的。

    ── 标量的判定（同理）────────────────────────────────────────────
      - 我相对 base 没改过 ⇒ 以**磁盘**为准（不覆盖更新的磁盘状态）
      - 只有我改过 ⇒ 以我为准
      - 双方都改过且不同 ⇒ 以磁盘为准（保守：宁可不动，也不覆盖磁盘上的新状态）

    `base` 为 None（不知道基线，例如手工构造的对象）⇒ 退化为旧行为"本副本优先"。
    """
    merged = dict(mine)
    base_pages = ((base or {}).get("pages") or {})
    base_fields = ((base or {}).get("fields") or {})

    mine_pages: dict = {}
    for p in (mine.get("pages") or []):
        mine_pages[_page_key(p)] = dict(p)
    disk_pages: dict = {}
    for p in (disk.get("pages") or []):
        disk_pages[_page_key(p)] = dict(p)

    # ① 磁盘删过、而我手里还留着的页 ⇒ 不复活
    dropped = {k for k in base_pages
               if k in mine_pages and k not in disk_pages}
    # ② 我删过、而磁盘上还留着的页 ⇒ 删除优先
    dropped |= {k for k in base_pages
                if k not in mine_pages and k in disk_pages}

    pages: list = []
    index: dict = {}
    for key, p in disk_pages.items():
        if key in dropped:
            continue
        index[key] = len(pages)
        pages.append(dict(p))
    for key, p in mine_pages.items():
        if key in dropped:
            continue
        if key in index:
            base_p = base_pages.get(key)
            disk_p = disk_pages[key]
            # 我没改过这一页（内容与基线一致）⇒ 保留磁盘上的版本，
            # 免得陈旧副本把磁盘上更新的纠偏角/手动旋转改回去
            if base_p is not None and p == base_p and disk_p != base_p:
                continue
            pages[index[key]] = dict(p)
        else:
            index[key] = len(pages)
            pages.append(dict(p))
    merged["pages"] = pages

    # 标量：三方合并（陈旧副本不得覆盖更新的磁盘状态）
    for f in _MERGE_FIELDS:
        if not base_fields:
            continue                       # 没有基线 ⇒ 保持"本副本赢"
        bv = base_fields.get(f)
        mv = mine.get(f)
        dv = disk.get(f)
        if mv == bv:
            merged[f] = dv                # 我没动过 ⇒ 磁盘的（更新的）值赢
        elif dv == bv:
            merged[f] = mv                # 只有我动过 ⇒ 我的值赢
        else:
            merged[f] = dv                # 双方都改过 ⇒ 以磁盘为准
            logger.warning("会话字段 %s 双方都改过，以磁盘为准: %s", f, dv)

    merged["created_at"] = disk.get("created_at", mine.get("created_at", 0.0))
    try:
        merged["updated_at"] = max(float(disk.get("updated_at") or 0.0),
                                   float(mine.get("updated_at") or 0.0))
    except (TypeError, ValueError):
        merged["updated_at"] = mine.get("updated_at", 0.0)
    return merged


def _job_ids_of(payload: dict) -> list:
    """从会话内容里取出去重后的 job_id 列表"""
    seen: set = set()
    result: list = []
    for p in (payload.get("pages") or []):
        job_id = p.get("job_id")
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        result.append(job_id)
    return result


def _protect_jobs(job_ids) -> None:
    """
    保护这些 job 的缓存目录（不被 eviction / cleanup_stale 清掉）。

    ★ `cache_manager.protect_dir` **早已落地**（cache_manager.py:305）
      ⇒ 这里**直接调用**：早年的"函数不存在就静默跳过"是死分支，
      函数一直都在，留着只会在保护真的失效时悄悄吞掉问题。
    ★ 保护失败只记 error 日志，**不抛给调用方**（调用方是会话落盘主路径）。
    """
    for job_id in job_ids:
        try:
            cache_manager.protect_dir(job_id)
        except Exception as e:
            logger.error("缓存目录保护失败: %s（%s）", job_id, e)


def _unprotect_jobs(job_ids) -> None:
    """
    解除这些 job 的缓存目录保护（会话结束 / 被删除时）。

    ★ `cache_manager.unprotect_dir` **早已落地**（cache_manager.py:326）
      ⇒ 直接调用（同上：存在性检查是死分支，已删）。
    """
    for job_id in job_ids:
        try:
            cache_manager.unprotect_dir(job_id)
        except Exception as e:
            logger.error("缓存目录解除保护失败: %s（%s）", job_id, e)


# ── 持久化 ──────────────────────────────────────────────────────────────


class SessionStore:
    """
    会话持久化（原子写 + 事务级锁：进程内线程锁 + 跨进程文件锁）。

    落盘位置：`<config_dir>/sessions/<session_id>.json`
    （目录由 `config_io.config_dir()` 派生 ⇒ 测试可整体重定向）
    """

    def __init__(self, base_dir: Optional[str] = None):
        self._base_dir = os.path.abspath(base_dir) if base_dir else None

    @property
    def base_dir(self) -> str:
        """会话目录（未显式指定时按 config_io.config_dir() 动态解析）"""
        if self._base_dir:
            return self._base_dir
        return sessions_dir()

    def path_for(self, session_id: str) -> str:
        """某个会话的文件路径"""
        sid = _validate_session_id(session_id)
        return os.path.join(self.base_dir, f"{sid}.json")

    # ── 读 ──

    def _read_payload(self, path: str) -> tuple[Optional[dict], bool]:
        """
        读会话文件，返回 (数据, 是否可以安全覆盖原文件)。

        - 文件不存在 → (None, True)：正常首次写入
        - 空文件     → (None, True)：没有任何数据可丢，视为可安全覆盖
        - 文件损坏（解析失败 / 编码乱码 / 顶层不是对象）→ 记 error 日志 +
          **备份原文**，返回 (None, False)：调用方**禁止**覆盖原文件
        - 读不出来（OSError）→ 返回 (None, False)：同样禁止覆盖
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return None, True
        except UnicodeDecodeError as e:
            logger.error("会话文件编码损坏: %s（%s）", path, e)
            _backup_corrupt(path)
            return None, False
        except OSError as e:
            logger.error("会话文件读取失败: %s（%s）", path, e)
            return None, False
        if not raw.strip():
            return None, True
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("会话文件损坏: %s（%s）", path, e)
            _backup_corrupt(path)
            return None, False
        if not isinstance(data, dict):
            logger.error("会话文件格式错误（期望对象）: %s", path)
            _backup_corrupt(path)
            return None, False
        return data, True

    def load(self, session_id: str) -> Optional[DocumentSession]:
        """
        读一个会话。

        返回 None 的情形（**原文件一律保留，绝不静默清空 / 删除**）：
        - 文件不存在
        - JSON 解析失败 / 编码乱码 / 顶层不是对象（已记 error 日志 + 备份原文）
        - 读不出来（OSError）
        - 内容不合法（未知 mode / state 等）
        """
        try:
            path = self.path_for(session_id)
        except ValueError as e:
            logger.error("%s", e)
            return None
        data, _overwritable = self._read_payload(path)
        if data is None:
            return None
        try:
            return DocumentSession.from_dict(data)
        except Exception as e:
            logger.error("会话文件内容不合法，已忽略（文件保留）: %s（%s）", path, e)
            return None

    def list_open_sessions(self) -> list:
        """列出所有**未结束**的会话（不含 completed / abandoned）"""
        result = []
        base = self.base_dir
        if not os.path.isdir(base):
            return result
        try:
            names = sorted(os.listdir(base))
        except OSError as e:
            logger.error("会话目录读取失败: %s（%s）", base, e)
            return result
        for name in names:
            if not name.endswith(".json"):
                continue
            session = self.load(name[:-len(".json")])
            if session is None:
                continue
            if session.state in TERMINAL_STATES:
                continue
            result.append(session)
        result.sort(key=lambda s: (s.created_at, s.session_id))
        return result

    # ── 写 ──

    def save(self, session: DocumentSession, merge: bool = True) -> None:
        """
        保存会话（原子写 + 跨进程排他锁，锁住整个"读-改-写"事务）。

        - `merge=True`（默认）：与磁盘上的现有内容做**三方合并**
          （base=本副本的基线 / mine=本副本 / theirs=磁盘当前）⇒
            ① 并发追加的批次**不丢**（两个 store 同时保存 ⇒ 两批页都在）；
            ② 磁盘上删掉的页**不会被陈旧副本复活**；
            ③ 磁盘上更新的状态（本副本没动过的字段）**不会被覆盖**。
          ⇒ "谁是陈旧副本"由**基线差异**判定，**不用 updated_at 时间戳**
            （并发下时间戳与内容新旧无关，且 Windows 分辨率约 15.6ms ⇒ 掷骰子）。
          ⇒ 删页：`remove_pages_from()` 置位的 `session.full_rewrite` 会被
            **逐页尊重**（我删掉的页不会因为磁盘上还留着就被保留下来）；
            直接手改 `session.pages` 而不走 remove_pages_from 仍不会删页。
        - `merge=False`：整体覆盖（丢弃磁盘上的旧内容）。
        - 原文件已损坏 ⇒ **跳过写入**（原文与备份都保留），绝不静默清空。
        - 写失败不抛异常（避免 GUI 因配置写失败而崩），只记 error 日志。
        """
        if session is None or not getattr(session, "session_id", ""):
            raise ValueError("session 必须带 session_id")
        path = self.path_for(session.session_id)
        os.makedirs(self.base_dir, exist_ok=True)
        payload = session.to_dict()
        base_snapshot = getattr(session, "base_snapshot", None)
        with _write_lock(path):
            try:
                with config_io.config_write_lock(path):
                    existing, overwritable = self._read_payload(path)
                    if not overwritable:
                        logger.error("会话原文件损坏，已跳过写入（原文与备份均保留）: %s",
                                     path)
                        return
                    if merge and existing is not None:
                        # ★ 三方合并（取代旧的"updated_at 比大小 + 整体覆盖"）。
                        #   旧逻辑的毛病：
                        #     ① updated_at 与"内容谁更新"无关 ⇒ 并发追加被整批丢弃；
                        #     ② 磁盘上 full_rewrite=True 时走"整体覆盖"分支，
                        #        "过期副本保护"根本没机会执行 ⇒ 陈旧副本把删掉的页复活。
                        disk_rw = bool(existing.get("full_rewrite"))
                        mem_rw = bool(getattr(session, "full_rewrite", False))
                        payload = _merge_payload(base_snapshot, payload, existing)
                        # 删页标志必须留在磁盘上：删过页的会话恒为 True，
                        # 陈旧副本保存**不得**把它清零（否则删除被复活）
                        payload["full_rewrite"] = bool(disk_rw or mem_rw)
                        if mem_rw:
                            logger.debug("会话发生过删页，已按删页意图逐页合并: %s", path)
                    config_io.atomic_write_json(path, payload)
                    # ★ 落盘成功 ⇒ 推进基线：此后"我读到的磁盘内容"= 我写上去的内容
                    #   （基线只含**我自己**的页 —— 别的写者追加的页不是我的，
                    #     不能算成"我删过"，否则下一次保存会把它们误删）
                    session.base_snapshot = _snapshot_of(session.to_dict())
            except config_io.ConfigLockTimeout as e:
                logger.error("会话保存被跳过（拿不到跨进程锁，本次未写入）: %s（%s）",
                             path, e)
                return
            except Exception as e:
                logger.error("会话保存失败: %s（%s）", path, e)
                return
        self._apply_protection(payload)

    def _apply_protection(self, payload: dict) -> None:
        """
        按会话状态维护缓存目录保护：

        - 活跃会话（scanning / awaiting_flip / paused）⇒ protect_dir 每个 job
        - 终态会话（completed / abandoned）⇒ unprotect_dir 每个 job
        """
        job_ids = _job_ids_of(payload)
        if not job_ids:
            return
        if payload.get("state") in TERMINAL_STATES:
            _unprotect_jobs(job_ids)
        else:
            _protect_jobs(job_ids)

    def release_protection(self, session: DocumentSession) -> None:
        """解除会话引用的所有 job 目录保护（B4-5 落盘完成后调用）"""
        _unprotect_jobs(session.referenced_job_ids)

    # ── 删 ──

    def delete(self, session_id: str) -> None:
        """删除会话文件（并解除它引用过的 job 目录保护）"""
        try:
            path = self.path_for(session_id)
        except ValueError as e:
            logger.error("%s", e)
            return
        session = self.load(session_id)
        if session is not None:
            _unprotect_jobs(session.referenced_job_ids)
        try:
            os.remove(path)
        except FileNotFoundError:
            logger.debug("会话文件不存在，无需删除: %s", path)
        except OSError as e:
            logger.error("会话文件删除失败: %s（%s）", path, e)
