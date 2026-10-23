"""出包清单（MANIFEST）必须与**本次**构建的产物一致 — HP Scan Tool v4.0

★ 缺陷（2026-10-24 实测）：
  `build_installer.py` 第③步只**自己算**安装包的字节数与 SHA256 并打印，
  **从不调用** `tools/verify_release.py` ⇒ 而 MANIFEST 只有那个脚本会生成
  ⇒ 于是重打安装包之后，`build/release/MANIFEST.txt` 里**还是上一次的**
  字节数与 SHA256（实测：清单 mtime 停在 10/23 10:13，内容也是那天的）
  ⇒ 谁看那份清单都会被误导（而清单本来就是给人看的）。

★ 本文件守两件事：
  ① **静态**：构建脚本必须调用清单的唯一生成者（`tools/verify_release.py`），
     且必须校验"清单里确实有本次的字节数与 SHA256"；
  ② **运行时（只读）**：如果本机有构建产物 ⇒ MANIFEST 必须与安装包一致。
"""
import hashlib
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = os.path.join(ROOT, "build", "release")
MANIFEST = os.path.join(RELEASE, "MANIFEST.txt")
BUILD_SCRIPT = os.path.join(ROOT, "build_installer.py")
VERIFY_SCRIPT = os.path.join(ROOT, "tools", "verify_release.py")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


class TestBuildScriptRegeneratesManifest:
    def test_build_script_calls_the_manifest_generator(self):
        src = open(BUILD_SCRIPT, encoding="utf-8").read()
        assert "verify_release.py" in src, \
            "build_installer.py 必须调用 tools/verify_release.py（MANIFEST 的唯一生成者）"

    def test_build_script_checks_manifest_content(self):
        src = open(BUILD_SCRIPT, encoding="utf-8").read()
        assert "MANIFEST.txt" in src, "构建脚本必须显式提到 MANIFEST.txt"
        # 必须**校验**清单内容（只生成不校验 ⇒ 清单坏了也没人知道）
        assert re.search(r"digest\s+not\s+in\s+_mtext", src) or \
            re.search(r"MANIFEST.*\bin\b.*\bnot\b", src), \
            "构建脚本必须校验清单里含有本次的字节数与 SHA256"

    def test_verify_release_is_the_manifest_owner(self):
        src = open(VERIFY_SCRIPT, encoding="utf-8").read()
        assert "MANIFEST.txt" in src, "verify_release.py 必须负责写 MANIFEST.txt"


class TestManifestMatchesArtifacts:
    def test_manifest_matches_the_installer(self):
        if not os.path.isdir(RELEASE):
            pytest.skip("本机没有构建产物")
        setups = sorted(f for f in os.listdir(RELEASE) if f.lower().endswith(".exe"))
        if not setups:
            pytest.skip("本机没有安装包")
        if not os.path.isfile(MANIFEST):
            pytest.fail("有安装包却没有 MANIFEST.txt ⇒ 清单没随构建生成")
        setup = os.path.join(RELEASE, setups[-1])
        size = os.path.getsize(setup)
        digest = sha256(setup)
        text = open(MANIFEST, encoding="utf-8", errors="replace").read()
        assert digest in text, \
            "MANIFEST 里的 SHA256 与实际安装包不符（清单过期了）⇒ 重新生成它"
        assert str(size) in text or "{:,}".format(size) in text, \
            "MANIFEST 里的字节数与实际安装包不符（清单过期了）⇒ 重新生成它"

    def test_manifest_is_not_older_than_the_installer(self):
        if not os.path.isdir(RELEASE):
            pytest.skip("本机没有构建产物")
        setups = sorted(f for f in os.listdir(RELEASE) if f.lower().endswith(".exe"))
        if not setups or not os.path.isfile(MANIFEST):
            pytest.skip("没有可比对的产物")
        setup = os.path.join(RELEASE, setups[-1])
        assert os.path.getmtime(MANIFEST) >= os.path.getmtime(setup) - 2, \
            "MANIFEST 比安装包还旧 ⇒ 清单是上一次构建留下的"

    def test_manifest_lists_the_portable_build(self):
        """★ 便携版（onefile）也是交付物 ⇒ 清单里必须有它。"""
        portable = os.path.join(ROOT, "dist", "HP_Scan_Tool.exe")
        if not os.path.isfile(portable) or not os.path.isfile(MANIFEST):
            pytest.skip("本机没有便携版产物")
        text = open(MANIFEST, encoding="utf-8", errors="replace").read()
        assert sha256(portable) in text, \
            "MANIFEST 里没有便携版的 SHA256 ⇒ 清单漏了「免安装备选」这一件"
