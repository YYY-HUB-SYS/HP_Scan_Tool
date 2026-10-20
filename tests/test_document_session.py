"""
document_session 单元测试 — HP Scan Tool v4.0（批次 4 · B4-1）

覆盖（brief §4.1 的 7 条必做 + 补充护栏）：
  1. 新建 → 保存 → 加载 ⇒ 字段逐个一致（往返）
  2. 状态机：每条合法转换通过；**每条非法转换抛错**
  3. 双面：扫完正面 ⇒ 落盘 ⇒ **重新加载** ⇒ 正面页还在、state == "awaiting_flip"
  4. 损坏的 JSON ⇒ 加载返回 None，且**原文件仍在**（不被删、不被静默清空）
  5. 两个 store 实例并发写同一会话 ⇒ 不丢更新
  6. add_pages 后 referenced_job_ids 去重正确
  7. force_keep / deskew_angle / manual_rotation 往返不丢

★ 测试**绝不碰真实 %APPDATA%**：本文件自己用 monkeypatch 把
  `config_io.config_dir` 指向 tmp_path（不依赖 conftest 的全局护栏兜底）。
"""

import json
import os
import threading

import pytest

import cache_manager
import config_io
import document_session
from document_session import (
    DocumentSession,
    InvalidTransitionError,
    PageRef,
    SessionStore,
    new_session,
)

# ── 状态机合法转换表（与 document_session._LEGAL_TRANSITIONS 对齐）──────
LEGAL_TRANSITIONS = {
    ("scanning", "awaiting_flip"),
    ("awaiting_flip", "scanning"),
    ("scanning", "paused"),
    ("paused", "scanning"),
    ("scanning", "completed"),
    ("paused", "completed"),
    ("awaiting_flip", "completed"),
    # 任意状态 → abandoned
    ("scanning", "abandoned"),
    ("awaiting_flip", "abandoned"),
    ("paused", "abandoned"),
    ("completed", "abandoned"),
}

ALL_STATE_PAIRS = {
    (a, b)
    for a in document_session.STATES
    for b in document_session.STATES
    if a != b
} | {
    (a, a)
    for a in document_session.STATES
    if a != "abandoned"
} | {("abandoned", "abandoned")}


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    """把 config_io.config_dir 指向 tmp_path —— 测试绝不碰真实 %APPDATA%"""
    base = tmp_path / "appdata" / "HP_Scan_Tool"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_io, "config_dir", lambda: str(base))
    return str(base)


@pytest.fixture
def store(sessions_root):
    return SessionStore()


def _make_session(mode="batch", **kwargs):
    s = new_session(mode=mode, **kwargs)
    return s


# ── 1. 往返 ─────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_save_then_load_preserves_every_field(self, store):
        s = new_session(mode="duplex", output_path=r"D:\out\doc.pdf", ext="pdf",
                        dpi=600, exposure={"brightness": 10, "contrast": -5},
                        reverse_back=False)
        s.add_pages("job_front", [r"C:\cache\job_front\page_001.jpg",
                                  r"C:\cache\job_front\page_002.jpg"], side="front")
        s.add_pages("job_back", [r"C:\cache\job_back\page_001.jpg"], side="back")

        store.save(s)
        loaded = store.load(s.session_id)

        assert loaded is not None
        assert loaded.session_id == s.session_id
        assert loaded.mode == s.mode
        assert loaded.state == s.state
        assert loaded.output_path == s.output_path
        assert loaded.ext == s.ext
        assert loaded.dpi == s.dpi
        assert loaded.exposure == s.exposure
        assert loaded.reverse_back == s.reverse_back
        assert loaded.created_at == s.created_at
        assert loaded.updated_at == s.updated_at
        assert loaded.total_pages == s.total_pages
        assert loaded.to_dict() == s.to_dict()

    def test_session_file_lives_under_config_dir_sessions(self, store, sessions_root):
        s = _make_session()
        store.save(s)
        expected = os.path.join(sessions_root, "sessions", f"{s.session_id}.json")
        assert os.path.exists(expected)
        assert store.path_for(s.session_id) == expected

    def test_load_missing_session_returns_none(self, store):
        assert store.load("sess_does_not_exist") is None


# ── 2. 状态机 ───────────────────────────────────────────────────────────


class TestStateMachine:
    @pytest.mark.parametrize("from_state,to_state", sorted(LEGAL_TRANSITIONS))
    def test_legal_transition_allowed(self, from_state, to_state):
        s = _make_session()
        s.state = from_state
        s.set_state(to_state)
        assert s.state == to_state

    @pytest.mark.parametrize("from_state,to_state",
                             sorted(ALL_STATE_PAIRS - LEGAL_TRANSITIONS))
    def test_illegal_transition_raises(self, from_state, to_state):
        s = _make_session()
        s.state = from_state
        with pytest.raises(InvalidTransitionError):
            s.set_state(to_state)
        assert s.state == from_state       # 非法转换不得改变状态

    def test_illegal_transition_is_also_a_value_error(self):
        s = _make_session()
        s.state = "completed"
        with pytest.raises(ValueError):
            s.set_state("scanning")

    def test_unknown_state_rejected(self):
        s = _make_session()
        with pytest.raises(ValueError):
            s.set_state("not_a_state")

    def test_module_level_can_transition_matches_instance(self):
        for from_state in document_session.STATES:
            for to_state in document_session.STATES:
                s = _make_session()
                s.state = from_state
                assert s.can_transition(to_state) == \
                    document_session.can_transition(from_state, to_state)

    def test_state_change_bumps_updated_at(self):
        s = _make_session()
        before = s.updated_at
        s.set_state("paused")
        assert s.updated_at >= before


# ── 3. 双面：正面落盘后重新加载 ─────────────────────────────────────────


class TestDuplexResume:
    def test_front_pages_survive_reload_with_awaiting_flip(self, store):
        s = new_session(mode="duplex", output_path="out.pdf", ext="pdf")
        s.add_pages("job_front", [r"C:\cache\job_front\page_001.jpg",
                                  r"C:\cache\job_front\page_002.jpg"], side="front")
        s.set_state("awaiting_flip")
        store.save(s)

        # 用**新的 store 实例**重新加载（模拟进程重启后续扫）
        reloaded = SessionStore().load(s.session_id)

        assert reloaded is not None
        assert reloaded.state == "awaiting_flip"
        assert reloaded.mode == "duplex"
        assert reloaded.total_pages == 2
        assert len(reloaded.front_pages) == 2
        assert len(reloaded.back_pages) == 0
        assert [p.cache_path for p in reloaded.front_pages] == \
            [r"C:\cache\job_front\page_001.jpg", r"C:\cache\job_front\page_002.jpg"]
        assert all(p.side == "front" for p in reloaded.pages)

    def test_back_batch_appends_after_reload(self, store):
        s = new_session(mode="duplex")
        s.add_pages("job_front", ["front1.jpg"], side="front")
        s.set_state("awaiting_flip")
        store.save(s)

        reloaded = SessionStore().load(s.session_id)
        reloaded.set_state("scanning")
        reloaded.add_pages("job_back", ["back1.jpg"], side="back")
        store.save(reloaded)

        final = SessionStore().load(s.session_id)
        assert final.total_pages == 2
        assert len(final.front_pages) == 1
        assert len(final.back_pages) == 1
        assert final.referenced_job_ids == ["job_front", "job_back"]


# ── 4. 损坏文件 ─────────────────────────────────────────────────────────


class TestCorruptFile:
    def test_corrupt_json_returns_none_and_keeps_file(self, store):
        sid = "sess_broken"
        path = store.path_for(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        original = "{ this is not json"
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

        assert store.load(sid) is None

        # 原文件仍在、内容未被清空
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == original

    def test_save_does_not_overwrite_corrupt_file(self, store):
        sid = "sess_broken2"
        path = store.path_for(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        original = "not json at all"
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

        s = _make_session(session_id=sid)
        store.save(s)

        # 损坏原文既没被删，也没被空会话覆盖
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == original

    def test_invalid_content_returns_none_and_keeps_file(self, store):
        sid = "sess_bad_state"
        path = store.path_for(sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        original = json.dumps({"session_id": sid, "mode": "single",
                               "state": "not_a_state"})
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

        assert store.load(sid) is None
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == original

    def test_corrupt_session_is_skipped_by_listing(self, store):
        path = store.path_for("sess_corrupt_list")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{oops")
        open_sessions = store.list_open_sessions()
        assert all(s.session_id != "sess_corrupt_list" for s in open_sessions)


# ── 5. 并发写不丢更新 ──────────────────────────────────────────────────


class TestConcurrentWrites:
    def test_two_stores_writing_same_session_keep_both_updates(self, store):
        sid = "sess_concurrent"
        base = new_session(mode="batch", session_id=sid)
        store.save(base)

        store_a = SessionStore()
        store_b = SessionStore()
        a = store_a.load(sid)
        b = store_b.load(sid)
        assert a is not None and b is not None

        a.add_pages("job_a", ["a1.jpg", "a2.jpg"], side="front")
        b.add_pages("job_b", ["b1.jpg", "b2.jpg"], side="back")

        barrier = threading.Barrier(2)

        def writer(st, session):
            barrier.wait()          # 让两个写事务真正重叠
            st.save(session)

        t1 = threading.Thread(target=writer, args=(store_a, a))
        t2 = threading.Thread(target=writer, args=(store_b, b))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive() and not t2.is_alive()

        final = SessionStore().load(sid)
        assert final is not None
        assert final.total_pages == 4
        assert set(final.referenced_job_ids) == {"job_a", "job_b"}

    def test_concurrent_writes_leave_valid_json(self, store):
        sid = "sess_concurrent_json"
        store.save(new_session(mode="batch", session_id=sid))
        store_a = SessionStore()
        store_b = SessionStore()
        barrier = threading.Barrier(2)

        def writer(st, tag):
            session = st.load(sid)
            session.add_pages(f"job_{tag}", [f"{tag}.jpg"])
            barrier.wait()
            st.save(session)

        t1 = threading.Thread(target=writer, args=(store_a, "x"))
        t2 = threading.Thread(target=writer, args=(store_b, "y"))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        with open(store.path_for(sid), "r", encoding="utf-8") as f:
            data = json.load(f)          # 必须仍是合法 JSON（原子替换）
        assert len(data["pages"]) == 2


# ── 6. job 去重 ─────────────────────────────────────────────────────────


class TestReferencedJobIds:
    def test_referenced_job_ids_deduplicated_in_order(self):
        s = _make_session()
        s.add_pages("job_a", ["a1.jpg"])
        s.add_pages("job_b", ["b1.jpg"])
        s.add_pages("job_a", ["a2.jpg"])
        s.add_pages("job_c", ["c1.jpg"])

        assert s.referenced_job_ids == ["job_a", "job_b", "job_c"]
        assert s.total_pages == 4

    def test_referenced_job_ids_empty_without_pages(self):
        assert _make_session().referenced_job_ids == []

    def test_page_no_continues_per_job(self):
        s = _make_session()
        s.add_pages("job_a", ["a1.jpg", "a2.jpg"])
        s.add_pages("job_a", ["a3.jpg"])
        s.add_pages("job_b", ["b1.jpg"])
        assert [p.page_no for p in s.pages] == [1, 2, 3, 1]

    def test_explicit_page_nos_are_used(self):
        s = _make_session()
        s.add_pages("job_a", ["a1.jpg", "a2.jpg"], page_nos=[7, 8])
        assert [p.page_no for p in s.pages] == [7, 8]

    def test_add_pages_rejects_unknown_side(self):
        s = _make_session()
        with pytest.raises(ValueError):
            s.add_pages("job_a", ["a1.jpg"], side="sideways")


# ── 7. 页参数往返 ───────────────────────────────────────────────────────


class TestPageParamsRoundTrip:
    def test_force_keep_deskew_rotation_survive_round_trip(self, store):
        s = _make_session()
        s.add_pages("job_x", ["x1.jpg", "x2.jpg"])
        s.pages[0].force_keep = True
        s.pages[0].deskew_angle = -2.75
        s.pages[0].manual_rotation = 90
        s.pages[1].deskew_angle = 3.5
        s.pages[1].manual_rotation = 270

        store.save(s)
        loaded = store.load(s.session_id)

        assert loaded is not None
        assert loaded.pages[0].force_keep is True
        assert loaded.pages[0].deskew_angle == pytest.approx(-2.75)
        assert loaded.pages[0].manual_rotation == 90
        assert loaded.pages[1].force_keep is False
        assert loaded.pages[1].deskew_angle == pytest.approx(3.5)
        assert loaded.pages[1].manual_rotation == 270

    def test_page_ref_defaults(self):
        p = PageRef(cache_path="p.jpg", job_id="job_a", page_no=1)
        assert p.side == "single"
        assert p.manual_rotation == 0
        assert p.deskew_angle == 0.0
        assert p.force_keep is False


# ── 补充护栏 ────────────────────────────────────────────────────────────


class TestSessionListing:
    def test_list_open_sessions_excludes_terminal(self, store):
        open_one = new_session(mode="batch", session_id="sess_open")
        done_one = new_session(mode="batch", session_id="sess_done")
        gone_one = new_session(mode="batch", session_id="sess_gone")
        done_one.set_state("paused")
        done_one.set_state("completed")
        gone_one.set_state("abandoned")
        for s in (open_one, done_one, gone_one):
            store.save(s)

        ids = [s.session_id for s in store.list_open_sessions()]
        assert "sess_open" in ids
        assert "sess_done" not in ids
        assert "sess_gone" not in ids

    def test_delete_removes_file(self, store):
        s = new_session(session_id="sess_to_delete")
        store.save(s)
        assert os.path.exists(store.path_for(s.session_id))
        store.delete(s.session_id)
        assert not os.path.exists(store.path_for(s.session_id))
        assert store.load(s.session_id) is None


class TestCacheProtectionContract:
    """与 B4-2（cache_manager.protect_dir / unprotect_dir）的接口契约"""

    def test_save_protects_each_referenced_job(self, store, monkeypatch):
        calls = []
        monkeypatch.setattr(cache_manager, "protect_dir",
                            lambda job_id: calls.append(job_id), raising=False)
        s = new_session(mode="batch")
        s.add_pages("job_a", ["a1.jpg"])
        s.add_pages("job_b", ["b1.jpg"])
        s.add_pages("job_a", ["a2.jpg"])
        store.save(s)
        assert calls == ["job_a", "job_b"]

    def test_completed_session_unprotects_jobs(self, store, monkeypatch):
        calls = []
        monkeypatch.setattr(cache_manager, "unprotect_dir",
                            lambda job_id: calls.append(job_id), raising=False)
        s = new_session(mode="batch")
        s.add_pages("job_a", ["a1.jpg"])
        s.set_state("paused")
        s.set_state("completed")
        store.save(s)
        assert calls == ["job_a"]

    def test_save_works_without_protect_dir(self, store, monkeypatch):
        """protect_dir 尚未落地（B4-2 并行任务）⇒ 不能因此失败"""
        monkeypatch.delattr(cache_manager, "protect_dir", raising=False)
        s = new_session(mode="batch")
        s.add_pages("job_a", ["a1.jpg"])
        store.save(s)
        assert store.load(s.session_id) is not None


class TestModuleConstraints:
    def test_no_qt_import(self):
        """纯 Python：不 import 任何 Qt（CLI 要能复用）"""
        src = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "document_session.py")
        with open(src, "r", encoding="utf-8") as f:
            text = f.read()
        assert "PySide6" not in text
        assert "tkinter" not in text

    def test_import_is_side_effect_free(self):
        """模块可被独立导入（不触网、不读真实用户目录）"""
        assert document_session.MODES == ("single", "duplex", "batch")
        assert set(document_session.STATES) == {
            "scanning", "awaiting_flip", "paused", "completed", "abandoned"}

    def test_session_id_is_filesystem_safe(self):
        sid = document_session.new_session_id()
        assert sid and not any(c in sid for c in '/\\:*?"<>|')
        assert SessionStore().path_for(sid).endswith(f"{sid}.json")

    def test_path_traversal_session_id_rejected(self, store):
        with pytest.raises(ValueError):
            store.path_for("../evil")


# ── 删页（"从第 N 页重扫"用）────────────────────────────────────────────


class TestRemovePagesFrom:
    """
    `SessionStore.save()` 的合并**只进不退**（报告 §3.4 的已知不足），
    所以"从第 N 页重扫"需要这个显式方法。

    语义：范围限定在**指定 job 内** —— 会话里其它 job（先前批次）的页不受影响。
    """

    @staticmethod
    def _mk_page(tmp_path, job_id, page_no):
        p = tmp_path / "cache" / job_id / f"page_{page_no:03d}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * (page_no * 10))
        return str(p)

    def test_removes_from_cut_within_that_job_only(self, tmp_path, store):
        s = _make_session(mode="batch")
        s.add_pages("jobA", [self._mk_page(tmp_path, "jobA", i)
                             for i in (1, 2, 3)])
        s.add_pages("jobB", [self._mk_page(tmp_path, "jobB", i)
                             for i in (1, 2)])
        assert s.total_pages == 5

        dropped = s.remove_pages_from("jobA", 2)
        assert [p.page_no for p in dropped] == [2, 3]
        assert s.total_pages == 3
        assert sorted(p.page_no for p in s.pages if p.job_id == "jobB") == [1, 2]
        assert sorted(p.page_no for p in s.pages if p.job_id == "jobA") == [1]

    def test_cut_beyond_last_page_removes_nothing(self, tmp_path, store):
        s = _make_session(mode="batch")
        s.add_pages("jobA", [self._mk_page(tmp_path, "jobA", 1)])
        dropped = s.remove_pages_from("jobA", 99)
        assert dropped == []
        assert s.total_pages == 1

    def test_drop_cache_removes_files_but_keeps_earlier_ones(self, tmp_path, store):
        s = _make_session(mode="batch")
        paths = [self._mk_page(tmp_path, "jobA", i) for i in (1, 2, 3)]
        s.add_pages("jobA", paths)
        s.remove_pages_from("jobA", 2)
        assert os.path.isfile(paths[0]), "第 1 页必须保留"
        assert not os.path.exists(paths[1]), "第 2 页的缓存文件必须删掉"
        assert not os.path.exists(paths[2]), "第 3 页的缓存文件必须删掉"

    def test_drop_cache_false_keeps_files(self, tmp_path, store):
        s = _make_session(mode="batch")
        paths = [self._mk_page(tmp_path, "jobA", i) for i in (1, 2)]
        s.add_pages("jobA", paths)
        s.remove_pages_from("jobA", 2, drop_cache=False)
        assert os.path.isfile(paths[1]), "drop_cache=False 时不该删文件"

    def test_survives_save_load_roundtrip(self, tmp_path, store):
        s = _make_session(mode="batch")
        s.add_pages("jobA", [self._mk_page(tmp_path, "jobA", i)
                             for i in (1, 2, 3)])
        s.remove_pages_from("jobA", 2)
        store.save(s)
        again = store.load(s.session_id)
        assert again is not None
        assert [p.page_no for p in again.pages] == [1]
