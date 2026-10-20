"""
scan_coordinator.compute_page_groups 单元测试 — HP Scan Tool v4.0

背景（N-7）：
  - 该函数此前「全仓零引用、零用例」，但覆盖矩阵却标着「✔ 覆盖」；
  - GUI 的 MultiPagePreviewDialog._build_groups() 曾与它重复实现同一套分组逻辑。

本文件覆盖：
  1. 无 splits / 单 split / 多 split / 重复 split / 空页列表
  2. 越界 split（负数、超出总页数）—— 当前实现的真实行为（见下方说明）
  3. 不变式：分组索引连续覆盖 0..total_pages-1 且不重不漏
  4. GUI._build_groups() 与协调器实现等价（重构前后一致性）

⚠ 关于「越界 split」的说明（重要）：
  compute_page_groups 当前**不做 clamp**。传入负数 split 会产出负下标，
  传入 > 总页数的 split 会产出超出页数的下标。
  这是**重构前就存在的既有缺陷**（旧 GUI 实现行为完全一致，见
  test_legacy_gui_impl_matches_new_impl），本轮是纯清理、不改行为，
  故此处按「现状」如实记录，并用 xfail 标出「期望的正确行为」，
  等协调器侧修复时该用例会 XPASS 并提醒同步更新。
  GUI 正常流程下 split 永远落在 (0, len(pages)) 内
  （见 MultiPagePreviewDialog._toggle_split），故该缺陷目前是潜伏的。
"""

import os
import tempfile

import pytest

import scan_coordinator
from scan_coordinator import compute_page_groups


# ────────── GUI 模块导入（与 tests/test_gui_consistency.py 同套路） ──────────

# 导入 GUI 模块会执行 setup_exception_logging()，往 ~/Desktop/hp_scan_debug.log 写日志。
# 测试期把 USERPROFILE 指向临时目录，避免污染用户桌面上的日志文件。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP_HOME = tempfile.mkdtemp(prefix="hp_groups_test_home_")
os.makedirs(os.path.join(_TMP_HOME, "Desktop"), exist_ok=True)
_PREV_USERPROFILE = os.environ.get("USERPROFILE")
os.environ["USERPROFILE"] = _TMP_HOME
try:
    import hp_scan_gui_pyside as gui
finally:
    if _PREV_USERPROFILE is not None:
        os.environ["USERPROFILE"] = _PREV_USERPROFILE
    else:
        os.environ.pop("USERPROFILE", None)


# ────────── 重构前的旧 GUI 实现（逐字取自重构前的 hp_scan_gui_pyside.py） ──────────

def _legacy_build_groups(splits, total_pages):
    """
    VERBATIM 复制自重构前的 MultiPagePreviewDialog._build_groups
    （git show HEAD:hp_scan_gui_pyside.py 中的 _build_groups 方法体，
      仅把 self.splits / self.pages 换成参数）。
    """
    if not splits:
        return [list(range(total_pages))]
    sorted_splits = sorted(splits)
    groups = []
    start = 0
    for split in sorted_splits:
        if split > start:
            groups.append(list(range(start, split)))
        start = split
    if start < total_pages:
        groups.append(list(range(start, total_pages)))
    return groups


# ────────── 1. 基本分组语义 ──────────

def test_no_splits_returns_one_group_with_all_pages():
    """无分割点 ⇒ 全部页在同一组"""
    assert compute_page_groups([], 3) == [[0, 1, 2]]


def test_no_splits_accepts_set_like_gui_stores():
    """GUI 里 splits 是 set（_toggle_split 用 set.add）⇒ 空 set 语义相同"""
    assert compute_page_groups(set(), 3) == [[0, 1, 2]]
    assert compute_page_groups({2}, 3) == [[0, 1], [2]]


def test_single_split():
    """单个分割点 ⇒ 切成两组"""
    assert compute_page_groups([2], 4) == [[0, 1], [2, 3]]


def test_multiple_splits():
    """多个分割点 ⇒ 切成多组"""
    assert compute_page_groups([1, 3], 5) == [[0], [1, 2], [3, 4]]


def test_unsorted_splits_give_same_result():
    """splits 无序（GUI 用 set，迭代序不定）⇒ 结果必须与有序一致"""
    assert compute_page_groups([3, 1], 5) == compute_page_groups([1, 3], 5)
    assert compute_page_groups([3, 1], 5) == [[0], [1, 2], [3, 4]]


def test_duplicate_splits_are_collapsed():
    """重复分割点不应产生空组或重复页"""
    assert compute_page_groups([2, 2], 5) == [[0, 1], [2, 3, 4]]
    assert compute_page_groups([2, 2, 2], 5) == [[0, 1], [2, 3, 4]]


def test_split_at_zero_is_noop():
    """split=0（第一页之前）⇒ 不切分（GUI 也禁止在第 1 页设分割点）"""
    assert compute_page_groups([0], 4) == [[0, 1, 2, 3]]


def test_split_at_total_pages_is_noop():
    """split == 总页数 ⇒ 落在最后一页之后 ⇒ 不额外切一组"""
    assert compute_page_groups([4], 4) == [[0, 1, 2, 3]]


# ────────── 2. 空页列表 ──────────

def test_empty_page_list_without_splits():
    """0 页、无分割点 ⇒ 一个空组（[[]]），不是崩溃也不是 None"""
    assert compute_page_groups([], 0) == [[]]


def test_empty_page_list_with_split():
    """0 页、有分割点：split=0 ⇒ 空分组列表"""
    assert compute_page_groups([0], 0) == []


def test_empty_page_list_with_out_of_range_split():
    """
    0 页 + 越界 split（> 0）⇒ 仍会产出越界下标（既有缺陷，现状锁定）。

    与 test_split_above_total_pages_is_not_clamped 是同一个缺陷在 0 页场景的体现。
    """
    assert compute_page_groups([1], 0) == [[0]]
    assert compute_page_groups([2], 0) == [[0, 1]]


# ────────── 3. 越界 split（现状记录，缺陷见模块 docstring） ──────────

def test_negative_split_is_not_clamped():
    """负数 split ⇒ 当前实现会产出负下标（既有缺陷，现状锁定）"""
    assert compute_page_groups([-1], 3) == [[-1, 0, 1, 2]]


def test_split_above_total_pages_is_not_clamped():
    """split > 总页数 ⇒ 当前实现会产出超出页数的下标（既有缺陷，现状锁定）"""
    assert compute_page_groups([5], 3) == [[0, 1, 2, 3, 4]]


def test_out_of_range_split_does_not_crash():
    """越界 split 至少不能抛异常 —— 返回结构合法"""
    for splits, total in (([-1], 3), ([99], 3), ([-5, 99], 4), ([1, 99], 4)):
        groups = compute_page_groups(splits, total)
        assert isinstance(groups, list)
        assert all(isinstance(g, list) for g in groups)
        assert all(isinstance(i, int) for g in groups for i in g)


@pytest.mark.xfail(
    reason="compute_page_groups 尚未 clamp 越界 split（重构前既有的缺陷，"
           "协调器侧修复后本用例会 XPASS，届时应同步更新上面的现状锁定用例）",
    strict=False,
)
def test_out_of_range_splits_should_be_clamped_to_page_range():
    """期望行为：越界 split 被 clamp ⇒ 索引始终落在 0..total_pages-1"""
    for splits, total in (([-1], 3), ([99], 3), ([-5, 99], 4)):
        groups = compute_page_groups(splits, total)
        flat = [i for g in groups for i in g]
        assert flat == list(range(total)), f"splits={splits} total={total} -> {groups}"


# ────────── 4. 不变式：连续覆盖 0..n-1、不重不漏 ──────────

_VALID_CASES = [
    ([], 0),
    ([], 1),
    ([], 5),
    ([0], 4),
    ([1], 4),
    ([2], 4),
    ([3], 4),
    ([4], 4),
    ([1, 3], 5),
    ([2], 5),
    ([1, 2, 3], 6),
    ([2, 2], 5),
    ([1, 1, 1], 5),
    ([0], 0),
    ([2], 2),
]


@pytest.mark.parametrize("splits,total", _VALID_CASES)
def test_groups_cover_all_pages_exactly_once(splits, total):
    """合法输入下：分组索引必须连续覆盖 0..total-1，不重不漏"""
    groups = compute_page_groups(splits, total)
    flat = [i for g in groups for i in g]
    assert flat == list(range(total)), f"splits={splits} total={total} -> {groups}"
    assert len(set(flat)) == len(flat), "存在重复页"


@pytest.mark.parametrize("splits,total", [c for c in _VALID_CASES if c[1] > 0])
def test_no_empty_group_in_output(splits, total):
    """合法输入下（至少 1 页）：不应产出空组

    ⚠ total=0 且无 split 时当前实现返回 [[]]（一个空组），
    这是「0 页」这一特殊情况，单独由 test_empty_page_list_without_splits 锁定。
    """
    groups = compute_page_groups(splits, total)
    assert all(len(g) > 0 for g in groups), f"splits={splits} total={total} -> {groups}"


@pytest.mark.parametrize("splits,total", _VALID_CASES)
def test_group_indices_are_non_negative_and_in_range(splits, total):
    """合法输入下：每个页下标都落在 0..total-1"""
    groups = compute_page_groups(splits, total)
    for g in groups:
        for i in g:
            assert 0 <= i < total, f"splits={splits} total={total} -> {groups}"


# ────────── 5. GUI._build_groups() 与协调器等价（重构前后一致性） ──────────

class _StubDialog:
    """MultiPagePreviewDialog 的最小替身：_build_groups 只用到 splits 与 pages"""

    def __init__(self, splits, pages):
        self.splits = splits
        self.pages = pages


_EQUIV_MATRIX = [
    ([], 0),
    ([], 4),
    ([1], 4),
    ([2], 4),
    ([1, 3], 5),
    ([2, 2], 5),
    ([0], 4),
    ([4], 4),
    ([-1], 3),      # 越界也要一致（既有缺陷，行为不得被重构改变）
    ([5], 3),
    ([1, 2, 3], 6),
]


@pytest.mark.parametrize("splits,total", _EQUIV_MATRIX)
def test_gui_build_groups_delegates_to_coordinator(splits, total):
    """GUI._build_groups() 必须与 scan_coordinator.compute_page_groups() 输出一致"""
    pages = list(range(1, total + 1))
    stub = _StubDialog(set(splits), pages)
    got = gui.MultiPagePreviewDialog._build_groups(stub)
    assert got == compute_page_groups(set(splits), total)


@pytest.mark.parametrize("splits,total", _EQUIV_MATRIX)
def test_legacy_gui_impl_matches_new_impl(splits, total):
    """
    重构前后一致性证明：旧 GUI 实现（逐字复制自重构前源码）与新实现（协调器）输出必须完全相同。

    覆盖正常值、重复 split、空页、以及负数/超总页数等越界值 —— 越界情况下
    两者同样「错得一样」，说明本次重构没有改变任何可观察行为。
    """
    pages = list(range(1, total + 1))
    stub = _StubDialog(set(splits), pages)
    new = gui.MultiPagePreviewDialog._build_groups(stub)
    old = _legacy_build_groups(set(splits), total)
    assert new == old, f"splits={splits} total={total} old={old} new={new}"


def test_gui_build_groups_uses_coordinator_function():
    """静态确认：GUI 不再自带一份分组实现，而是调用协调器那份"""
    import inspect
    src = inspect.getsource(gui.MultiPagePreviewDialog._build_groups)
    assert "compute_page_groups" in src
    assert "sorted_splits" not in src, "GUI 里不应再保留重复的分组实现"
