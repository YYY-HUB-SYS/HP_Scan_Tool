"""
会话「幽灵会话」回归 — HP Scan Tool v4.0

缺陷背景
--------
旧收尾顺序是 **先删 job 目录（清缓存）→ 再把会话状态置 completed**
（状态写发生在 GUI 主线程、且**失败只记日志**）⇒ 中途崩溃 / 写锁失败 /
磁盘写失败会留下一个**磁盘上仍是「可重试」状态、但页路径已经指向被删掉的
缓存**的会话 ⇒ 用户重试时全是死路径（幽灵会话），而且**用户不知情**。

新契约（save_manager 负责，纯数据上报，**不 import Qt**）
----------------------------------------------------------
1. 落盘成功 ⇒ **先把会话状态写成 completed（并回读验证）**，**再**清缓存
   ⇒ 顺序反了才会出现"指向死路径的会话"
2. 状态写失败 ⇒ **不报告 completed**：
   - 会话留在**可重试**状态（scanning / paused / awaiting_flip）
   - 缓存**保留**（页还在 ⇒ 重试能成）
   - 返回值上如实上报：`status_write_failed` / `status_write_error`
3. 既有行为不得退化：落盘成功后 job 目录**照旧被清掉**、保护被解除。

★ 不跑真机扫描、不写真实用户数据（conftest 已把 APPDATA 重定向到沙箱）。
"""

import os

import pytest
from PIL import Image

import cache_manager
import config_io
import document_session
import history_manager
import page_meta
import save_manager
from document_session import new_session


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """隔离的缓存根目录（缓存件写在真 job 目录里 ⇒ 落盘后应被清掉）"""
    root = str(tmp_path / "cache")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: root)
    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: os.path.join(root, "cache_config.json"))
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(cache_manager, "_active_jobs", set())
    monkeypatch.setattr(cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(cache_manager, "_cache_size_cache", None)
    hist = str(tmp_path / "history.json")
    monkeypatch.setattr(history_manager, "_history_file", lambda: hist)
    monkeypatch.setattr(history_manager, "HISTORY_FILE", None)
    return root


# ── 工具 ──────────────────────────────────────────────────────────────


def _write_job_pages(cache_root, job_id, pages=2):
    """往缓存里写一个 job 的若干页（真 job 目录 ⇒ 落盘后应被清掉）"""
    import io

    paths = {}
    for page_no in range(1, pages + 1):
        buf = io.BytesIO()
        Image.new("RGB", (600, 800), (220, 30, 30)).save(buf, "JPEG")
        path = cache_manager.write_page(job_id, page_no, buf.getvalue(), "jpg")
        with Image.open(io.BytesIO(buf.getvalue())) as im:
            width, height = im.size
        page_meta.PageMetaStore(cache_root).set_page(
            job_id, page_no,
            page_meta.PageMeta(cache_path=path, width=width, height=height,
                               manual_rotation=0, deskew_angle=0.0))
        paths[page_no] = path
    return paths


def _make_session(cache_root, job="job_ghost", pages=2, ext="pdf"):
    paths = _write_job_pages(cache_root, job, pages)
    session = new_session(mode="single", ext=ext, dpi=300)
    session.add_pages(job, [paths[i] for i in sorted(paths)],
                      side="single", page_nos=list(range(1, pages + 1)))
    return session, paths


def _break_session_writes(monkeypatch):
    """让**会话文件**的写全部失败（其它文件不受影响）"""
    real = config_io.atomic_write_json

    def _boom(path, data):
        if os.path.basename(os.path.dirname(path)) == "sessions":
            raise OSError("磁盘满（测试注入）")
        return real(path, data)

    monkeypatch.setattr(config_io, "atomic_write_json", _boom)


# ── A：顺序 —— 先写状态，再清缓存 ─────────────────────────────────────


class TestStateBeforeCacheCleanup:
    def test_state_is_completed_before_cache_is_removed(self, tmp_path,
                                                         cache_root, monkeypatch):
        """★ 顺序护栏：删缓存的那一刻，磁盘上的会话状态必须**已经是 completed**。

        顺序反了 ⇒ 崩溃窗口里磁盘上的会话还"开着"，而它的页已经被删 ⇒ 幽灵会话。
        """
        session, _paths = _make_session(cache_root)
        document_session.SessionStore().save(session)     # 先落盘会话（GUI 的常态）

        seen = []
        real_remove = cache_manager.remove_job

        def _spy(jid):
            on_disk = document_session.SessionStore().load(session.session_id)
            seen.append(None if on_disk is None else on_disk.state)
            return real_remove(jid)

        monkeypatch.setattr(cache_manager, "remove_job", _spy)

        result = save_manager.save_session(session, str(tmp_path / "out.pdf"))

        assert len(result) == 1
        assert seen, "根本没有走到删缓存这一步"
        assert seen == ["completed"], \
            f"删缓存时磁盘上的会话状态还是 {seen}（顺序反了 ⇒ 会留幽灵会话）"

    def test_success_still_clears_cache_and_marks_completed(self, tmp_path,
                                                             cache_root):
        """★ 既有行为不得退化：落盘成功 ⇒ job 目录被清、保护解除、状态=completed。"""
        session, paths = _make_session(cache_root)
        cache_manager.protect_dir("job_ghost")
        assert cache_manager.is_job_protected("job_ghost")

        result = save_manager.save_session(session, str(tmp_path / "out.pdf"))

        assert len(result) == 1 and os.path.exists(result[0])
        assert result.status_write_failed is False
        assert not os.path.isdir(os.path.join(cache_root, "job_ghost")), \
            "落盘成功后 job 目录必须被清掉"
        assert not cache_manager.is_job_protected("job_ghost")
        on_disk = document_session.SessionStore().load(session.session_id)
        assert on_disk is not None and on_disk.state == "completed"


# ── B：状态写失败 ⇒ 不报告 completed，且不留幽灵会话 ──────────────────


class TestStateWriteFailure:
    def test_state_write_failure_is_reported(self, tmp_path, cache_root,
                                             monkeypatch):
        """★ 状态写失败 ⇒ 返回值上如实上报（纯数据字段，GUI 侧直接读）。"""
        session, _paths = _make_session(cache_root)
        document_session.SessionStore().save(session)
        _break_session_writes(monkeypatch)

        result = save_manager.save_session(session, str(tmp_path / "out.pdf"))

        assert getattr(result, "status_write_failed", None) is True, \
            "状态写失败必须在返回值里如实上报"
        assert getattr(result, "status_write_error", ""), \
            "状态写失败必须带上原因文本"

    def test_state_write_failure_keeps_cache_for_retry(self, tmp_path,
                                                       cache_root, monkeypatch):
        """★ 状态写失败 ⇒ 缓存**保留**（页还在 ⇒ 可重试），会话不置 completed。"""
        session, paths = _make_session(cache_root)
        document_session.SessionStore().save(session)
        _break_session_writes(monkeypatch)

        result = save_manager.save_session(session, str(tmp_path / "out.pdf"))

        # 产物文件本身是写出来了的（用户拿到了文件）
        assert len(result) == 1 and os.path.exists(result[0])
        # 缓存保留
        assert os.path.isdir(os.path.join(cache_root, "job_ghost")), \
            "状态写失败时缓存必须保留（可重试）"
        for path in paths.values():
            assert os.path.exists(path), "缓存页不见了"
        # 磁盘上的会话**不是** completed
        on_disk = document_session.SessionStore().load(session.session_id)
        assert on_disk is not None
        assert on_disk.state != "completed", \
            "状态没写成却报告 completed ⇒ 谎报"
        assert on_disk.state in ("scanning", "paused", "awaiting_flip"), \
            f"会话应留在可重试状态，实际 {on_disk.state}"

    def test_no_ghost_session_after_state_write_failure(self, tmp_path,
                                                        cache_root, monkeypatch):
        """★★ 幽灵会话的判据：会话引用的**每个**缓存路径都还活着。

        「磁盘上可重试 + 页路径已死」= 幽灵会话 ⇒ 这里必须保证路径都还在。
        """
        session, _paths = _make_session(cache_root)
        document_session.SessionStore().save(session)
        _break_session_writes(monkeypatch)

        save_manager.save_session(session, str(tmp_path / "out.pdf"))

        on_disk = document_session.SessionStore().load(session.session_id)
        assert on_disk is not None
        assert on_disk.state != "completed"
        assert on_disk.pages, "会话页面列表不该是空的"
        for page in on_disk.pages:
            assert page.cache_path and os.path.exists(page.cache_path), \
                "幽灵会话：页路径指向死文件 %s" % page.cache_path

    def test_in_memory_session_stays_retryable(self, tmp_path, cache_root,
                                               monkeypatch):
        """状态写失败 ⇒ 内存里的会话也必须留在可重试态（不能只改磁盘）。"""
        session, _paths = _make_session(cache_root)
        document_session.SessionStore().save(session)
        _break_session_writes(monkeypatch)

        save_manager.save_session(session, str(tmp_path / "out.pdf"))

        assert session.state in ("scanning", "paused", "awaiting_flip"), \
            f"内存里的会话状态是 {session.state}（不可重试）"

    def test_partial_page_failure_keeps_state_retryable(self, tmp_path,
                                                        cache_root):
        """★ 有页失败 ⇒ 缓存保留（既有行为）且会话**不是** completed。"""
        session, paths = _make_session(cache_root)
        document_session.SessionStore().save(session)
        # 把第 2 页的缓存件换成坏字节 ⇒ 该页转换失败
        with open(paths[2], "wb") as f:
            f.write(b"not-an-image")

        result = save_manager.save_session(session, str(tmp_path / "partial.pdf"))

        assert result.failed_pages == 1
        assert os.path.isdir(os.path.join(cache_root, "job_ghost")), \
            "有页失败时缓存必须保留（可重试）"
        on_disk = document_session.SessionStore().load(session.session_id)
        assert on_disk is not None and on_disk.state != "completed"


# ── C：返回值的向后兼容 ───────────────────────────────────────────────


class TestResultContract:
    def test_default_result_has_no_status_failure(self):
        """既有调用方（save_pages / 老代码）拿到的结果里这两个字段是安全的默认值。"""
        result = save_manager.SaveResult(["a.jpg"])
        assert result.status_write_failed is False
        assert result.status_write_error == ""
        assert result.ok is True
        assert list(result) == ["a.jpg"]

    def test_save_manager_does_not_import_qt(self):
        """★ save_manager 不得 import Qt（CLI 也要能用）。"""
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "save_manager.py"),
            encoding="utf-8").read()
        for bad in ("PySide6", "from PySide", "import PySide"):
            assert bad not in src, f"save_manager 里出现了 {bad}"
