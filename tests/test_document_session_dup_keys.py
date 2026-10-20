r"""
会话合并键重复 ⇒ 落盘静默丢页 的回归测试 — HP Scan Tool v4.0

★ 缺陷背景（终审 Important I-1，实测）
--------------------------------------------------------------------------
会话页的**合并键**是 `(job_id, page_no, side)`。`DocumentSession.add_pages`
允许调用方**显式传入与已有页重复的页号** ⇒ 两个页的键相同
⇒ 落盘时三方合并把页列表建成 `dict` ⇒ **后者覆盖前者**
⇒ **静默丢页，没有任何报错**（终审实测：3 页 → 2 页）。

⇒ 修法：在**入口**（`add_pages`）就把重复键改掉 ——
不抛异常（GUI 槽里抛异常会打断流程）、不丢页、并记 warning。

另有一条**隐患**（同一根因的下游）：GUI 的 `_page_no_from_path` 只认
`page_` 前缀，而 `save_manager` 的正则 `page_(\d+)\.` 连 `crop_page_003.jpg`
都认 ⇒ 同一文件两处解析出**不同页号**（0 vs 3）⇒ 多个解析不出的页会**全是 0**
⇒ 又是重复键。本文件也钉住这条口径一致性。

（★ 本 docstring 是 **raw string**：里面的 `\d` 是要展示给读者看的正则片段，
不是 Python 转义序列 —— 非 raw 会触发 `SyntaxWarning: invalid escape sequence`。）
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import document_session  # noqa: E402
from document_session import DocumentSession, PageRef  # noqa: E402


def _session():
    s = DocumentSession(session_id="sess_dup", mode="batch")
    return s


class TestDuplicateMergeKeys:

    def test_add_pages_with_duplicate_page_no_keeps_all_pages(self):
        """★ 显式传入重复页号 ⇒ 页**一个都不能少**。"""
        s = _session()
        s.add_pages("job_a", ["p1.jpg"], page_nos=[1])
        s.add_pages("job_a", ["p2.jpg"], page_nos=[1])   # ← 重复键
        assert len(s.pages) == 2, "重复页号不应让页消失"

    def test_duplicate_page_no_gets_a_different_merge_key(self):
        """★ 重复键必须被**改掉**（否则落盘时会被 dict 覆盖）。"""
        s = _session()
        s.add_pages("job_a", ["p1.jpg"], page_nos=[1])
        s.add_pages("job_a", ["p2.jpg"], page_nos=[1])
        keys = {(p.job_id, p.page_no, p.side) for p in s.pages}
        assert len(keys) == len(s.pages), (
            "合并键重复 ⇒ 落盘会静默丢页: %s" % sorted(keys))

    def test_duplicate_inside_one_batch_is_also_fixed(self):
        """★ 同一批里两个相同页号 ⇒ 也要改掉。"""
        s = _session()
        s.add_pages("job_b", ["p1.jpg", "p2.jpg"], page_nos=[2, 2])
        keys = {(p.job_id, p.page_no, p.side) for p in s.pages}
        assert len(keys) == 2, "同批重复页号也要改: %s" % sorted(keys)

    def test_same_page_no_different_side_is_allowed(self):
        """★ 正面/背面**同号**是合法的（双面扫描的语义）⇒ 不能误改。"""
        s = _session()
        s.add_pages("job_c", ["front.jpg"], side="front", page_nos=[1])
        s.add_pages("job_c", ["back.jpg"], side="back", page_nos=[1])
        assert len(s.pages) == 2
        assert {(p.job_id, p.page_no, p.side) for p in s.pages} == {
            ("job_c", 1, "front"), ("job_c", 1, "back")}

    def test_different_job_same_page_no_is_allowed(self):
        """★ 不同 job 同号是合法的（每个 job 自己从 1 开始编号）。"""
        s = _session()
        s.add_pages("job_d", ["a.jpg"], page_nos=[1])
        s.add_pages("job_e", ["b.jpg"], page_nos=[1])
        assert len(s.pages) == 2


class TestDuplicateKeysSurviveRoundTrip:
    """★ 端到端：重复键的页，**落盘再读回**后一个都不能少。"""

    def test_save_and_reload_keeps_every_page(self, tmp_path, monkeypatch):
        store = document_session.SessionStore(base_dir=str(tmp_path / "sess"))
        s = DocumentSession(session_id="sess_roundtrip", mode="batch")
        s.add_pages("job_x", ["p1.jpg", "p2.jpg", "p3.jpg"], page_nos=[1, 1, 2])
        assert len(s.pages) == 3

        store.save(s)
        reloaded = store.load("sess_roundtrip")
        assert reloaded is not None
        assert len(reloaded.pages) == 3, (
            "落盘后页数变少 ⇒ 合并键重复被静默覆盖了: %s"
            % [p.page_no for p in reloaded.pages])

    def test_save_twice_keeps_every_page(self, tmp_path):
        """★ 终审实测的原始症状：第一次存 3 页，第二次存 2 页。"""
        store = document_session.SessionStore(base_dir=str(tmp_path / "sess2"))
        s = DocumentSession(session_id="sess_twice", mode="batch")
        s.add_pages("job_y", ["p1.jpg", "p2.jpg", "p3.jpg"], page_nos=[1, 1, 2])

        store.save(s)
        after_first = store.load("sess_twice")
        assert after_first is not None
        assert len(after_first.pages) == 3, (
            "第一次落盘就丢了页: %s" % [p.page_no for p in after_first.pages])

        store.save(after_first)
        after_second = store.load("sess_twice")
        assert after_second is not None
        assert len(after_second.pages) == 3, (
            "第二次落盘丢了页（终审 I-1 的原始症状）: %s"
            % [p.page_no for p in after_second.pages])


class TestPageNoParsingConsistency:
    """★ 两处页号解析口径必须一致（否则会产生重复键）。"""

    def test_gui_and_save_manager_agree_on_crop_page_names(self):
        """`crop_page_003.jpg` ⇒ 两处都必须解析出 3。"""
        import hp_scan_gui_pyside as gui
        import save_manager

        name = "crop_page_003.jpg"
        gui_no = gui._page_no_from_path(name)
        sm_no = save_manager._page_no_from_path(name)
        assert gui_no == sm_no, (
            "两处页号解析口径不一致 ⇒ 同一文件解析出不同页号 ⇒ 重复键: "
            "gui=%s save_manager=%s" % (gui_no, sm_no))

    def test_both_parse_plain_page_names(self):
        import hp_scan_gui_pyside as gui
        import save_manager

        name = "page_007.jpg"
        assert gui._page_no_from_path(name) == 7
        assert save_manager._page_no_from_path(name) == 7
