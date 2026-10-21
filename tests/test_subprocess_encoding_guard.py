"""
守护测试：`text=True` 的子进程调用**必须显式指定 encoding** —— 回归护栏

★ 缺陷（2026-10-21 收口时实测抓到）
---------------------------------------------------------------------------
`subprocess.run(..., capture_output=True, text=True)` **不指定 `encoding`** 时，
父进程按**控制台代码页（本机 GBK）**解码子进程的输出 ⇒ 子进程里只要出现
**GBK 解不了的字节序列**（UTF-8 中文就是典型），`subprocess` 的 `_readerthread`
就抛 `UnicodeDecodeError` ⇒ 该异常**逃出线程** ⇒ pytest 记一条
`PytestUnhandledThreadExceptionWarning`。

★ 为什么它是「偶发的」（本轮实测 8 轮全量里出现 3 次）
  取决于子进程**实际输出内容**里是否恰好出现解不了的字节序列
  ⇒ 用例大多时好时坏 ⇒ **最容易被当成噪声忽略**，也就最容易盖住真告警。

★ 修法
  凡是 `text=True` / `universal_newlines=True` 的调用，一律配
  `encoding="utf-8", errors="replace"`（与本仓库既有的
  `tests/conftest.py`、`hp_order_robustness.py` 同一口径）。

★ 本文件是**静态守护**（AST 扫描 `tests/` 下所有 `.py`），不是运行期观察 ——
  运行期只能等到它偶发一次，而 AST 扫描是**每次都判**。
"""
import ast
import os

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

#: 会把子进程输出**解码成文本**的关键字参数
_TEXT_KEYS = ("text", "universal_newlines")

#: 真正起子进程的调用
_SUBPROCESS_FUNCS = ("run", "Popen", "check_output", "check_call", "call")


def _iter_test_files():
    for name in sorted(os.listdir(TESTS_DIR)):
        if name.endswith(".py"):
            yield os.path.join(TESTS_DIR, name)


def _keyword(call):
    """取一个 Call 的关键字参数名集合（不区分 `**kwargs` 展开）。"""
    return {kw.arg for kw in call.keywords if kw.arg}


def _text_call_sites(path):
    """返回该文件里「用文本模式起子进程」的调用点（行号 + 是否带 encoding）。"""
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=path)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "subprocess":
            continue
        if func.attr not in _SUBPROCESS_FUNCS:
            continue
        kws = _keyword(node)
        if not any(k in kws for k in _TEXT_KEYS):
            continue
        hits.append((node.lineno, "encoding" in kws))
    return hits


def test_guard_file_itself_parses():
    """自检：本文件能被 AST 解析（守护逻辑本身别写错）。"""
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
        ast.parse(fh.read(), filename=__file__)


@pytest.mark.parametrize("path", list(_iter_test_files()),
                         ids=[os.path.basename(p) for p in _iter_test_files()])
def test_text_mode_subprocess_must_set_encoding(path):
    """★ `text=True` 的子进程调用必须同时带 `encoding=`。"""
    offenders = []
    for lineno, has_encoding in _text_call_sites(path):
        if not has_encoding:
            offenders.append(lineno)
    assert not offenders, (
        "%s 第 %s 行：`subprocess.*(..., text=True)` 没有指定 `encoding=` ⇒ "
        "父进程按控制台代码页（GBK）解码子进程的 UTF-8 输出 ⇒ "
        "_readerthread 抛 UnicodeDecodeError ⇒ 泄漏成 "
        "PytestUnhandledThreadExceptionWarning（偶发）"
        % (os.path.basename(path), offenders))


def test_guard_detects_a_violating_snippet():
    """★ 自检：守护判据**抓得住**违规写法（不能是空转断言）。"""
    bad = (
        "import subprocess\n"
        "subprocess.run(['x'], capture_output=True, text=True, timeout=1)\n"
    )
    tree = ast.parse(bad)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "subprocess"
             and n.func.attr == "run"]
    assert calls, "样例本身没解析出 subprocess.run 调用"
    kws = _keyword(calls[0])
    assert "text" in kws and "encoding" not in kws, (
        "样例必须是「text=True 且没有 encoding」的违规形态，实际 %r" % kws)


def test_guard_accepts_a_conforming_snippet():
    """★ 反向自检：合规写法（含 encoding）**不该**被判违规。"""
    good = (
        "import subprocess\n"
        "subprocess.run(['x'], capture_output=True, text=True, timeout=1,\n"
        "               encoding='utf-8', errors='replace')\n"
    )
    tree = ast.parse(good)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "subprocess"]
    kws = _keyword(calls[0])
    assert "text" in kws and "encoding" in kws, (
        "样例必须是「text=True 且带 encoding」的合规形态，实际 %r" % kws)
