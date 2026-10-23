r"""
打安装包 — HP Scan Tool v4.0

流程：
  ① `build_exe.py --onedir` ⇒ `build/onedir/HP_Scan_Tool/`（**目录发布**：真实文件，不解压 %TEMP%）
  ② Inno Setup（ISCC）编译 `installer/hp_scan_tool.iss` ⇒ `build/release/HP_Scan_Tool_Setup_v4.0.0.0.exe`
  ③ 出包自检（清单 + sha256 + 完整性）⇒ 缺件/不一致直接失败，不产出半成品

用法：
  python build_installer.py                 # 全流程
  python build_installer.py --skip-build    # 只重打安装包（onedir 产物已在）
  python build_installer.py --skip-inno     # 只做 onedir 构建（机器没装 Inno Setup 时）

★ 为什么保留 onefile 便携版
  onefile 单文件（`dist/HP_Scan_Tool.exe`）免安装、拷到哪都能跑，但**每次启动**
  要把 138.5 MiB 解压到 `%TEMP%\_MEIxxxxxx`（实测启动 3.3 秒），崩溃/强杀还会
  **残留** 138.5 MiB/次，且别人"清空 %TEMP%"会打断正在运行的程序。
  ⇒ **正式部署形态 = 安装包（onedir，零 %TEMP% 占用）**；onefile 保留给免安装场景。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import time

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
ONEDIR_DIR = os.path.join(HERE, "build", "onedir", "HP_Scan_Tool")
RELEASE_DIR = os.path.join(HERE, "build", "release")
ISS = os.path.join(HERE, "installer", "hp_scan_tool.iss")

# Inno Setup 编译器（标准安装路径；也可用环境变量 ISCC 覆盖）
ISCC_CANDIDATES = [
    os.environ.get("ISCC", ""),
    r"C:\Program Files\Inno Setup 7\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
]


def find_iscc():
    for p in ISCC_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def run(cmd, cwd=None):
    print("\n$ %s" % " ".join(cmd), flush=True)
    t0 = time.time()
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if line.strip():
            print("  " + line[:200], flush=True)
    print("  rc=%d 用时 %.1fs" % (r.returncode, time.time() - t0), flush=True)
    return r.returncode


skip_build = "--skip-build" in sys.argv
skip_inno = "--skip-inno" in sys.argv

print("=" * 64)
print("  HP Scan Tool — 安装包构建")
print("=" * 64)

# ── ① onedir 目录发布 ─────────────────────────────────────────────────
if not skip_build:
    print("\n=== ① onedir 目录发布 ===", flush=True)
    if run([PY, os.path.join(HERE, "build_exe.py"), "--onedir"], cwd=HERE) != 0:
        print("!! onedir 构建失败")
        sys.exit(1)
else:
    print("\n=== ① onedir 目录发布（跳过，用现有产物） ===", flush=True)

if not os.path.isdir(ONEDIR_DIR):
    print("!! onedir 产物不存在: %s" % ONEDIR_DIR)
    sys.exit(1)
exe = os.path.join(ONEDIR_DIR, "HP_Scan_Tool.exe")
if not os.path.isfile(exe):
    print("!! 主程序不存在: %s" % exe)
    sys.exit(1)
_n = _size = 0
for root, _d, files in os.walk(ONEDIR_DIR):
    for f in files:
        _n += 1
        try:
            _size += os.path.getsize(os.path.join(root, f))
        except OSError:
            pass
print("  产物: %d 个文件 / %.1f MiB" % (_n, _size / 1048576.0))

if skip_inno:
    print("\n（--skip-inno：只做完 onedir，不打安装包）")
    sys.exit(0)

# ── ② Inno Setup 编译 ─────────────────────────────────────────────────
iscc = find_iscc()
if not iscc:
    print("\n!! 找不到 Inno Setup 编译器（ISCC.exe）。")
    print("   装 Inno Setup 6.3+ 后重跑，或用 --skip-inno 只出 onedir 目录。")
    sys.exit(2)
print("\n=== ② Inno Setup 编译 ===", flush=True)
print("  ISCC: %s" % iscc)
os.makedirs(RELEASE_DIR, exist_ok=True)
if run([iscc, ISS], cwd=HERE) != 0:
    print("!! ISCC 编译失败（看上面的报错）")
    sys.exit(1)

# ── ③ 出包自检 ─────────────────────────────────────────────────────────
print("\n=== ③ 出包自检 ===", flush=True)
setups = sorted(f for f in os.listdir(RELEASE_DIR)
                if f.lower().endswith(".exe"))
if not setups:
    print("!! 没产出安装包")
    sys.exit(1)
setup = os.path.join(RELEASE_DIR, setups[-1])
size = os.path.getsize(setup)
digest = sha256(setup)
print("  安装包: %s" % setup)
print("    字节: %d（%.1f MiB）" % (size, size / 1048576.0))
print("    SHA256: %s" % digest)
if size < 5 * 1024 * 1024:
    print("!! 安装包只有 %.1f MiB ⇒ 多半没装上运行库，**不出半成品**" % (size / 1048576.0))
    sys.exit(1)

# ★ 2026-10-24：清单（build/release/MANIFEST.txt）必须**跟着这次构建**重生成。
#   实测过：重打安装包后，清单里**还是上一次**的字节数与 SHA256
#   （MANIFEST 只有手动跑 tools/verify_release.py 才会更新，而本脚本从没调用它）
#   ⇒ 谁看那份清单都会被误导。verify_release.py 是 MANIFEST 的唯一生成者
#   ⇒ 这里直接调它（顺带把 onedir 完整性/残留检查也过一遍）。
print("\n=== ③' 出包清单（MANIFEST）重生成 ===", flush=True)
if run([PY, os.path.join(HERE, "tools", "verify_release.py"), "--quiet"], cwd=HERE) != 0:
    print("!! 清单重生成失败（tools/verify_release.py）⇒ **不出半成品**")
    sys.exit(1)
_manifest = os.path.join(RELEASE_DIR, "MANIFEST.txt")
if os.path.isfile(_manifest):
    _mtext = open(_manifest, encoding="utf-8", errors="replace").read()
    if (digest not in _mtext) or (str(size) not in _mtext):
        print("!! 清单里没有本次的字节数/SHA256 ⇒ 清单不可信")
        sys.exit(1)
    print("  清单已含本次构建的字节数与 SHA256 ✔")
else:
    print("!! 清单文件没生成 ⇒ **不出半成品**")
    sys.exit(1)

print("\n=== 完成 ===")
print("  安装包: %s" % setup)
print("  下一步：")
print("    · 静默安装自检: %s /VERYSILENT /SUPPRESSMSGBOXES" % setup)
print("    · 卸载自检    : %s /VERYSILENT /SUPPRESSMSGBOXES" % setup)
print("  ⇒ 建议交付时给用户**安装包**；onefile 便携版（dist\\HP_Scan_Tool.exe）作为免安装备选。")
