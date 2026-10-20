"""
escl_engine 死代码清理的守护测试 — HP Scan Tool v4.0

背景（收口核销 verify-closeout.md §5.3 / V0-N-7）：
    `escl_engine.scan_to_file` 挂在 `__all__` 上对外宣称可用，实际**全仓零调用**
    （生产代码、tests、tools 都没有调用方），而且它自带一套 PDF / 扩展名合成逻辑，
    与 `save_manager` 的 PDF 合成路径**重复且规格不一致**（轮 1 核销 V1 记为既有缺陷）。
    拍板：**删除**（桌面应用的内部模块，`__all__` 不是对终端用户的公开契约）。

本文件把「它是死代码」这个判断**锁死** —— 一旦有人重新定义 / 重新导出 /
重新调用它，本文件立刻变红，避免它被再次误用（尤其是与 save_manager 走两套 PDF 路径）。
"""

import re
from pathlib import Path

import escl_engine


REPO_ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

# 需要纳入「零调用」扫描的源码范围：生产代码根目录 + 测试 + 工具脚本
SCAN_DIRS = ("tests", "tools")
SCAN_GLOBS = ("*.py", "**/*.py")


def _iter_python_sources():
    """产出仓库内需要检查的 .py 文件（不含本守护文件自身）。"""
    for path in sorted(REPO_ROOT.glob("*.py")):
        if path.resolve() != SELF:
            yield path
    for sub in SCAN_DIRS:
        base = REPO_ROOT / sub
        if not base.is_dir():
            continue
        for pattern in SCAN_GLOBS:
            for path in sorted(base.glob(pattern)):
                if path.is_file() and path.resolve() != SELF:
                    yield path


def test_scan_to_file_not_exported():
    """`scan_to_file` 不得再出现在 `__all__` 里（不得对外宣称可用）。"""
    assert "scan_to_file" not in escl_engine.__all__


def test_scan_to_file_attribute_absent():
    """`escl_engine.scan_to_file` 必须真的不存在 —— 不是只从 `__all__` 里摘掉名字。"""
    assert not hasattr(escl_engine, "scan_to_file")


def test_dunder_all_has_no_dangling_export():
    """`__all__` 里列出的每个名字都必须在模块上真实存在（防悬空导出）。"""
    missing = [name for name in escl_engine.__all__ if not hasattr(escl_engine, name)]
    assert missing == [], f"__all__ 里有悬空导出: {missing}"


def test_no_python_source_references_scan_to_file():
    """全仓 .py（生产代码 + tests + tools）不得再出现 `scan_to_file` —— 零调用方才是死代码。"""
    offenders = []
    for path in _iter_python_sources():
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if re.search(r"\bscan_to_file\b", text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"仍有 .py 引用 scan_to_file: {offenders}"


def test_escl_engine_imports_cleanly():
    """删除死代码后模块仍可正常 import（冒烟）。"""
    assert escl_engine.__name__ == "escl_engine"
    assert callable(escl_engine.execute_scan)
    assert callable(escl_engine.execute_multipage_scan)
