"""
双面 / 批次合并算法 — HP Scan Tool v4.0
把多个页序列按规则合并成最终页序。

设计约束（批次 4 · B4-3）
------------------------------------------------------------------
1. **纯函数**：零 IO、零依赖、零 Qt。
   本模块**不** import PySide6 / tkinter，也**不** import 仓库里任何其它模块
   ⇒ CLI 与 GUI 可共用，单测也不必拉起 GUI 依赖。
2. **输入不被修改**：调用后 front / back / pages / batches 本身内容不变。
   反转背面时用 list(reversed(...)) 拷贝，**绝不**用 x.reverse()（那会就地改原列表）。
3. **接受任意序列**：list / tuple 等不可变序列都能直接传进来。
4. **算法口诀：先反转、再交错、多余的接末尾**（见 interleave_duplex）。

双面合并的物理背景
------------------------------------------------------------------
ADF 双面扫描时正面与背面是**分两次**扫的：
  · 正面按扫入顺序：F1, F2, F3, ...
  · 背面那一叠被**整叠翻面**再送进机器 ⇒ 扫入顺序是反的：扫到的第 1 张背面
    其实是 F3 的背面，最后扫到的才是 F1 的背面。
所以背面默认（reverse_back=True）要先反转，再与正面一一交错。

单双面混扫（末页只有一面）时两边页数可能不等：
  · 正面多出（末页只有正面）⇒ 多出的正面页**按原序接在末尾**；
  · 背面多出（多扫了一张背面）⇒ 多出的背面页同样接在末尾。
两种情况互斥（只有一边会多出），所以"谁接在末尾"没有歧义。
"""

__all__ = ["interleave_duplex", "merge_batches", "group_pages"]


def interleave_duplex(front, back, reverse_back=True):
    """
    双面交错合并 —— **先反转、再交错、多余的接末尾**。

    :param front: 正面页序列（list / tuple 均可）
    :param back: 背面页序列（list / tuple 均可）
    :param reverse_back: 背面是否整叠翻面导致顺序相反
        · True（默认）：先反转 back 再交错 —— 真实 ADF 双面扫描的默认行为
        · False：back 顺序本来就正常，**仍然交错**（不是拼接）
    :return: 合并后的页序列（list）

    ★ 输入不被修改：front / back 会被拷贝后再处理，原对象内容不变。
    """
    # 拷贝一份再处理：既保证"输入不被修改"，也顺带支持 tuple 等不可变序列
    front = list(front)
    back = list(back)

    # ① 先反转（list(reversed(...)) 是拷贝，不会就地改原列表）
    if reverse_back:
        back = list(reversed(back))

    # ② 再交错：F1, B_last, F2, B_{last-1}, ...
    merged = []
    pairs = min(len(front), len(back))
    for i in range(pairs):
        merged.append(front[i])
        merged.append(back[i])

    # ③ 多余的接末尾（正面多出时按原序；两边只可能一边多出）
    merged.extend(front[pairs:])
    merged.extend(back[pairs:])

    return merged


def merge_batches(batches):
    """
    按批次顺序拼接页列表（不反转、不交错）。

    :param batches: 批次序列，每个批次是一个页序列
    :return: 合并后的页序列（list）

    · 空输入 ⇒ []
    · 空的批次项 ⇒ 跳过（不产生空洞）
    · 不改顺序
    · ★ 输入不被修改（batches 及其内部列表都不动）
    """
    merged = []
    for batch in batches:
        if not batch:          # 空批次（以及 None）⇒ 跳过，不留空洞
            continue
        merged.extend(batch)
    return merged


def group_pages(pages, group_size=None):
    """
    按固定大小分组（用于大批量：每 N 页一组）。

    :param pages: 页序列（list / tuple 均可）
    :param group_size: 每组页数；None ⇒ 不分组，返回 [全部页]
    :return: 分组列表（list[list]）

    · 7 页 + group_size=3 ⇒ 3 组，页数分布 [3, 3, 1]
    · 空输入 ⇒ []（没有页就没有组）
    · group_size 必须为正整数，否则抛 ValueError
    · ★ 输入不被修改
    """
    pages = list(pages)
    if not pages:
        return []
    if group_size is None:
        return [pages]
    if group_size <= 0:
        raise ValueError("group_size 必须为正整数: %r" % (group_size,))
    return [pages[i:i + group_size] for i in range(0, len(pages), group_size)]