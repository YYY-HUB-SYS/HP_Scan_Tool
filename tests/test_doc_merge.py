"""
doc_merge 单元测试 — HP Scan Tool v4.0
覆盖: 双面交错合并（brief §3.1 边界表**逐行**断言，两种 reverse_back 各测一遍）
      + 批次合并边界 / 分组 / 输入不被修改 / tuple 输入
"""

import pytest

from doc_merge import group_pages, interleave_duplex, merge_batches

# ── brief §3.1 边界表（原样照抄，一行不漏）───────────────────────────────
# (front, back, reverse_back=True 的期望, reverse_back=False 的期望, 用例名)
_DUPLEX_ROWS = [
    ([1, 2, 3], ["a", "b", "c"], [1, "c", 2, "b", 3, "a"], [1, "a", 2, "b", 3, "c"], "equal_len"),
    ([1, 2, 3], ["a", "b"], [1, "b", 2, "a", 3], [1, "a", 2, "b", 3], "front_extra"),
    ([1, 2], ["a", "b", "c"], [1, "c", 2, "b", "a"], [1, "a", 2, "b", "c"], "back_extra"),
    ([], ["a", "b"], ["b", "a"], ["a", "b"], "no_front"),
    ([1], [], [1], [1], "no_back"),
    ([], [], [], [], "both_empty"),
]


class TestInterleaveDuplex:
    """双面交错合并 —— brief §3.1 边界表逐行断言"""

    @pytest.mark.parametrize(
        "front,back,expected,_keep,_name",
        _DUPLEX_ROWS,
        ids=[row[4] for row in _DUPLEX_ROWS],
    )
    def test_boundary_table_reverse_back_true(self, front, back, expected, _keep, _name):
        """reverse_back=True：先反转背面，再交错，多余的接末尾"""
        assert interleave_duplex(front, back, reverse_back=True) == expected

    @pytest.mark.parametrize(
        "front,back,_rev,expected,_name",
        _DUPLEX_ROWS,
        ids=[row[4] for row in _DUPLEX_ROWS],
    )
    def test_boundary_table_reverse_back_false(self, front, back, _rev, expected, _name):
        """reverse_back=False：背面不反转，但**仍然交错**（不是拼接）"""
        assert interleave_duplex(front, back, reverse_back=False) == expected

    def test_default_is_reverse_back_true(self):
        """默认参数就是 reverse_back=True（背面整叠翻面）"""
        front = [1, 2, 3]
        back = ["a", "b", "c"]
        assert interleave_duplex(front, back) == interleave_duplex(front, back, True)

    def test_inputs_not_modified(self):
        """★ 输入不被修改：反转用的是拷贝，不是 x.reverse()"""
        front = [1, 2, 3]
        back = ["a", "b", "c"]
        interleave_duplex(front, back, reverse_back=True)
        assert front == [1, 2, 3]
        assert back == ["a", "b", "c"]
        interleave_duplex(front, back, reverse_back=False)
        assert front == [1, 2, 3]
        assert back == ["a", "b", "c"]

    def test_accepts_tuples(self):
        """传入 tuple（不可变序列）也要能工作，不报错"""
        assert interleave_duplex((1, 2, 3), ("a", "b", "c")) == [1, "c", 2, "b", 3, "a"]
        assert interleave_duplex((1, 2, 3), ("a", "b"), reverse_back=False) == [1, "a", 2, "b", 3]
        assert interleave_duplex((), ()) == []


class TestMergeBatches:
    """批次合并 —— 顺序拼接，不反转不交错"""

    def test_empty_input(self):
        """空输入 ⇒ []"""
        assert merge_batches([]) == []

    def test_skips_empty_batches(self):
        """空的批次项 ⇒ 跳过（不产生空洞）"""
        assert merge_batches([[1, 2], [], [3]]) == [1, 2, 3]
        assert merge_batches([[], []]) == []

    def test_preserves_order(self):
        """不改顺序"""
        assert merge_batches([[3, 1], [2], [4, 5]]) == [3, 1, 2, 4, 5]

    def test_single_batch(self):
        """单批次"""
        assert merge_batches([[1, 2, 3]]) == [1, 2, 3]

    def test_inputs_not_modified(self):
        """★ 输入不被修改"""
        batches = [[1, 2], [], [3]]
        merged = merge_batches(batches)
        assert merged == [1, 2, 3]
        assert batches == [[1, 2], [], [3]]

    def test_accepts_tuples(self):
        """传入 tuple（不可变序列）也要能工作"""
        assert merge_batches([(1, 2), (3,)]) == [1, 2, 3]


class TestGroupPages:
    """按固定大小分组（大批量）"""

    def test_group_size_3_seven_pages(self):
        """7 页 + group_size=3 ⇒ 分组页数分布 [3, 3, 1]"""
        pages = list(range(7))
        groups = group_pages(pages, group_size=3)
        assert [len(g) for g in groups] == [3, 3, 1]
        assert groups == [[0, 1, 2], [3, 4, 5], [6]]

    def test_group_size_none_no_grouping(self):
        """group_size=None ⇒ 不分组，返回 [全部页]"""
        pages = [1, 2, 3, 4]
        assert group_pages(pages) == [[1, 2, 3, 4]]
        assert group_pages(pages, group_size=None) == [[1, 2, 3, 4]]

    def test_empty_pages(self):
        """空输入 ⇒ []（没有页就没有组）"""
        assert group_pages([]) == []
        assert group_pages([], group_size=3) == []

    def test_group_size_one(self):
        """每页自成一组"""
        assert group_pages([1, 2, 3], group_size=1) == [[1], [2], [3]]

    def test_group_size_larger_than_pages(self):
        """组大小超过总页数 ⇒ 只有一组"""
        assert group_pages([1, 2], group_size=10) == [[1, 2]]

    def test_invalid_group_size_raises(self):
        """group_size 必须为正整数"""
        with pytest.raises(ValueError):
            group_pages([1, 2], group_size=0)
        with pytest.raises(ValueError):
            group_pages([1, 2], group_size=-1)

    def test_inputs_not_modified(self):
        """★ 输入不被修改"""
        pages = [1, 2, 3, 4]
        group_pages(pages, group_size=3)
        assert pages == [1, 2, 3, 4]

    def test_accepts_tuple(self):
        """传入 tuple（不可变序列）也要能工作"""
        assert group_pages((1, 2, 3), group_size=2) == [[1, 2], [3]]
