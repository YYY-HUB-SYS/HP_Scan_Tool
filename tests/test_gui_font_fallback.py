"""
GUI 字体回退契约测试 — HP Scan Tool v4.0

锁死 **2026-10-22 用户在另一台电脑上实测出的真缺陷**：
「整个 UI 的文字全部是方块/乱码」。

**根因（三条）**
  ① 字体族被**写死**成 "Microsoft YaHei UI" —— 那是 Windows **简体中文版**
     才带的字体，英文版 / 精简版 Windows 根本没装；
  ② 仓库里带了 10.5 MB 的思源黑体 `NotoSansSC-Regular.ttf`，但以前的
     `load_embedded_font()` 只把它**注册**进字体库、**从来没被用上**；
  ③ QSS 里又写死了 `font-family` 栈，**盖掉** `app.setFont()`。

**为什么本地测不出来**：开发/测试机是中文 Windows（有 Microsoft YaHei UI）
⇒ 界面完全正常 ⇒ 全部本地测试都绿 ⇒ 只有拿到别人的机器上才暴露。
⇒ 本文件的作用就是把这个"只在别人机器上暴露"的性质**锁死在仓库里**。

**判据为什么用「存在性」而不是「能不能渲染汉字」**（实测 2026-10-22）：
  · `QFontMetrics.inFont(ch)` 对**根本不存在的字体**也返回 True
    （Qt 会静默回退到某个默认字体）⇒ 假阳性；
  · `QRawFont.supportsCharacter(ch)` 在 Windows 平台上一律返回 False
    ⇒ 假阴性。
⇒ 两个都不能当判据。**存在性**才可靠：字体库里没有 ⇒ 一定画不出汉字；
  而 YaHei UI / SimSun / Noto Sans SC 这些**本身就是中文字体**，存在就能画。
"""

import ast
import io
import os
import re
import struct

import pytest

import hp_scan_gui_pyside as gui


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QSS_PATH = os.path.join(REPO_ROOT, "resources", "styles", "app.qss")
GUI_PATH = os.path.join(REPO_ROOT, "hp_scan_gui_pyside.py")
FONT_PATH = os.path.join(REPO_ROOT, "resources", "fonts",
                         "NotoSansSC-Regular.ttf")


# ──────────────────── 内嵌字体的 cmap 解析（与机器无关）────────────────────
# ★ 为什么自己解析 TTF：Qt 的高层 API（inFont / supportsCharacter）都不可靠
#   （实测：对不存在的字体 inFont 也返回 True；supportsCharacter 一律 False）
#   ⇒ 唯一能证明「字体自带汉字」的办法是**看字体文件本身**。

def _u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def font_cmap_codes(path):
    """返回字体文件里**有字形**的码位集合（支持 cmap 格式 0/4/6/12）"""
    b = open(path, "rb").read()
    base = _u32(b, 12) if b[:4] == b"ttcf" else 0
    num_tables = _u16(b, base + 4)
    tables = {}
    for i in range(num_tables):
        off = base + 12 + 16 * i
        tag = b[off:off + 4].decode("latin-1")
        tables[tag] = (_u32(b, off + 8), _u32(b, off + 12))
    assert "cmap" in tables, "字体没有 cmap 表: %s" % path
    coff = tables["cmap"][0]
    codes = set()
    for i in range(_u16(b, coff + 2)):
        rec = coff + 4 + 8 * i
        off = coff + _u32(b, rec + 4)
        fmt = _u16(b, off)
        if fmt == 4:
            seg_x2 = _u16(b, off + 6)
            seg = seg_x2 // 2
            ends = off + 14
            starts = ends + seg_x2 + 2
            deltas = starts + seg_x2
            ranges = deltas + seg_x2
            for s in range(seg):
                end = _u16(b, ends + 2 * s)
                start = _u16(b, starts + 2 * s)
                delta = _u16(b, deltas + 2 * s)
                ro = _u16(b, ranges + 2 * s)
                if start == 0xFFFF:
                    continue
                for c in range(start, min(end, 0xFFFE) + 1):
                    if ro == 0:
                        gid = (c + delta) & 0xFFFF
                    else:
                        gi = ranges + 2 * s + ro + 2 * (c - start)
                        if gi + 1 >= len(b):
                            continue
                        gid = _u16(b, gi)
                        if gid:
                            gid = (gid + delta) & 0xFFFF
                    if gid:
                        codes.add(c)
        elif fmt == 12:
            for g in range(_u32(b, off + 12)):
                go = off + 16 + 12 * g
                start = _u32(b, go)
                end = min(_u32(b, go + 4), start + 0x20000)
                codes.update(range(start, end + 1))
        elif fmt == 6:
            first = _u16(b, off + 6)
            for c in range(_u16(b, off + 8)):
                if _u16(b, off + 10 + 2 * c):
                    codes.add(first + c)
        elif fmt == 0:
            for c in range(256):
                if b[off + 6 + c]:
                    codes.add(c)
    return codes


class TestEmbeddedFontIsReallyRegistered:
    def test_embedded_font_loads_and_reports_a_family(self):
        """内嵌思源黑体必须能加载，并**返回族名**（旧实现只注册不返回 ⇒ 没人用）"""
        fam = gui.load_embedded_font()
        assert fam, "内嵌字体没加载出来（resources/fonts/NotoSansSC-Regular.ttf）"
        assert isinstance(fam, str) and fam.strip(), "必须返回字体族名"
        assert gui._EMBEDDED_FONT_FAMILY == fam

    def test_embedded_family_is_in_the_font_database(self):
        """注册后它必须真的出现在字体库里（否则设了也白设）"""
        fam = gui.load_embedded_font()
        assert fam in set(gui.QFontDatabase.families())

    def test_load_embedded_font_is_idempotent(self):
        """重复调用不得重复注册（否则白占一份 10 MB 字体内存）"""
        first = gui.load_embedded_font()
        second = gui.load_embedded_font()
        assert first == second


class TestEmbeddedFontReallyHasChineseGlyphs:
    """★ 与机器无关的判定：直接看**字体文件**的字形表

    ★ 为什么必须这样做：开发机是中文 Windows ⇒ 任何字体族名都能渲染出汉字
      ⇒ 比对渲染像素毫无意义（本机两种字体渲染结果完全相同）。
      ⇒ 唯一能证明「内嵌字体自带汉字」的办法是解析字体文件的 cmap。
    """

    def test_embedded_font_file_exists_and_has_cmap(self):
        assert os.path.isfile(FONT_PATH), "内嵌字体文件不见了: %s" % FONT_PATH
        codes = font_cmap_codes(FONT_PATH)
        assert len(codes) > 20000, "字形数量异常少: %d" % len(codes)

    def test_embedded_font_covers_common_chinese(self):
        codes = font_cmap_codes(FONT_PATH)
        probe = "扫描仪设置保存输稿器墨粉黑白打印机文件页面数量开始停止取消错误"
        missing = [ch for ch in probe if ord(ch) not in codes]
        assert not missing, "内嵌字体缺这些字: %s" % "".join(missing)

    def test_embedded_font_covers_every_chinese_char_used_in_the_ui(self):
        """★ 自维护式守护：UI 里用到的**每一个**汉字都必须在字形表里

        ⇒ 以后有人加了带生僻字的界面文案，这条测试立刻红 ⇒ 不会到别人
          机器上才发现是方块。
        """
        codes = font_cmap_codes(FONT_PATH)
        with io.open(GUI_PATH, "r", encoding="utf-8") as fh:
            src = fh.read()
        with io.open(QSS_PATH, "r", encoding="utf-8") as fh:
            qss = fh.read()
        chars = set(re.findall(r"[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]",
                               src + qss))
        assert chars, "没扫到任何汉字（守护本身失效了）"
        missing = sorted(ch for ch in chars if ord(ch) not in codes)
        assert not missing, "内嵌字体缺 UI 用到的这些字: %s" % "".join(missing)


class TestResolveFontFamily:
    def test_prefers_system_cjk_font_when_present(self, monkeypatch):
        """机器上有 Microsoft YaHei UI ⇒ 就用它（观感与系统一致）"""
        monkeypatch.setattr(gui, "font_is_available",
                            lambda fam: fam == "Microsoft YaHei UI")
        fam, note = gui.resolve_font_family()
        assert fam == "Microsoft YaHei UI"
        assert "系统字体" in note

    def test_falls_back_to_embedded_font_when_no_system_cjk(self, monkeypatch):
        """★ 核心判据：机器上**没有**任何系统中文字体 ⇒ 必须用**内嵌**思源黑体

        ★ 这正是用户那台机器的情形（界面全是方块）。
        """
        embedded = gui.load_embedded_font()
        # 模拟「英文版 Windows」：只有我们自己注册进去的内嵌字体在库里
        monkeypatch.setattr(gui, "font_is_available",
                            lambda fam: fam == embedded)
        fam, note = gui.resolve_font_family()
        assert fam == embedded, "没有系统中文字体时必须用内嵌字体"
        assert "内嵌字体" in note

    def test_never_returns_a_family_outside_the_allowlist(self, monkeypatch):
        """★ 终审判据：选出来的族必须是「白名单里的中文字体」或「内嵌字体」

        ⇒ **绝不能**落到 Segoe UI / Arial 这类「在字体库里但没有汉字字形」的族
        ⇒ 也就不会出现满屏方块。
        """
        embedded = gui.load_embedded_font()
        allowed = set(gui._FONT_PREFERRED) | {embedded}
        # 各种极端字体库都试一遍
        for fake in ([], ["Arial"], ["Segoe UI", "Tahoma"],
                     ["Arial", gui._EMBEDDED_FONT_FAMILY or "x"],
                     list(gui._FONT_PREFERRED)):
            monkeypatch.setattr(gui, "font_is_available",
                                lambda fam, _f=fake: fam in _f)
            fam, _note = gui.resolve_font_family()
            assert fam in allowed, "选出了不在白名单里的字体: %r" % fam

    def test_resolve_result_is_usable_for_widgets(self, qt_app):
        """解析出来的族必须能被 QFont 接受（不能是空串/None）"""
        fam, _note = gui.resolve_font_family()
        assert fam
        f = gui.QFont(fam, gui._FONT_SIZE)
        assert f.family() == fam or f.family()


class TestQssFontInjection:
    def test_inject_puts_chosen_family_first(self):
        qss = 'QWidget { font-family: "Microsoft YaHei UI", "Segoe UI"; }'
        out = gui.inject_font_family(qss, "Noto Sans SC")
        assert 'font-family: "Noto Sans SC"' in out
        assert out.index('"Noto Sans SC"') < out.index('"Microsoft YaHei UI"')

    def test_real_qss_font_family_is_overridden(self):
        """★ 真 QSS：注入后**首个**字体族必须是我们选中的那个

        ★ 为什么必须改 QSS：QSS 里的 `font-family` 会**盖掉** `app.setFont()`
        ⇒ 只设应用字体不生效。
        """
        qss = gui.load_stylesheet()
        assert qss, "app.qss 读不到"
        assert "font-family" in qss, "app.qss 里必须有 font-family 声明"
        out = gui.inject_font_family(qss, "Noto Sans SC")
        first = out.split("font-family:")[1].split(";")[0]
        assert first.strip().startswith('"Noto Sans SC"'), (
            "QSS 的首个字体族没被替换: %r" % first)

    def test_inject_is_a_noop_without_input(self):
        assert gui.inject_font_family("", "X") == ""
        assert gui.inject_font_family("abc", "") == "abc"


class TestMainWiringOrder:
    """★ 顺序也是契约：QSS 会盖掉 app.setFont() ⇒ 必须**先定字体再套样式**"""

    def _main_ast(self):
        with io.open(os.path.join(REPO_ROOT, "hp_scan_gui_pyside.py"),
                     "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                return node
        raise AssertionError("找不到 main()")

    def test_main_sets_application_font(self):
        node = self._main_ast()
        calls = [ast.unparse(n) for n in ast.walk(node)
                 if isinstance(n, ast.Call)]
        assert any("setFont" in c for c in calls), "main() 必须调用 app.setFont()"

    def test_main_resolves_font_before_applying_stylesheet(self):
        node = self._main_ast()
        seq = []
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                txt = ast.unparse(n)
                if "resolve_font_family" in txt:
                    seq.append(("resolve", txt))
                elif "setStyleSheet" in txt:
                    seq.append(("stylesheet", txt))
        order = [k for k, _ in seq]
        assert "resolve" in order, "main() 必须调用 resolve_font_family()"
        assert "stylesheet" in order, "main() 必须设置样式表"
        assert order.index("resolve") < order.index("stylesheet"), (
            "必须**先**定字体再套样式表（QSS 会盖掉 app.setFont()）：%s" % order)
