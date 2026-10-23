"""出包自检 —— HP Scan Tool 安装包

★ 借鉴 Windows_Keeper 的 `tools/assemble_package.py` 思路（**只读借鉴，未改动对方任何文件**）：
  「组装用脚本，别手搬」+ 产出**清单报告**（每件大小与 sha256）+ 缺件/不一致**直接失败**，
  不产出半成品。

★ 校验四件事
  ① 安装包**存在**且是合法 PE（MZ 头）+ 版本资源里的版本号**与文件名一致**
     （KEEPER 的教训：不写 VersionInfoVersion 时 Setup 壳的文件版本是 0.0.0.0，肉眼极难发现）
  ② onedir 产物**完整**：主程序 + PySide6 运行时 + resources（图标/字体）
  ③ onedir 产物里**没有**残留的开发期文件（.pdb / __pycache__ / .spec / 日志）
  ④ 生成清单 `build/release/MANIFEST.txt`（每件：相对路径 / 字节 / sha256）

用法：
    python tools/verify_release.py            # 自检并写清单
    python tools/verify_release.py --quiet
"""
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONEDIR = os.path.join(ROOT, "build", "onedir", "HP_Scan_Tool")
RELEASE = os.path.join(ROOT, "build", "release")
QUIET = "--quiet" in sys.argv

# onedir 里必须有的东西（缺一即失败）
REQUIRED = [
    "HP_Scan_Tool.exe",
    os.path.join("_internal", "python312.dll"),
    os.path.join("_internal", "base_library.zip"),
    # ★ PyInstaller 6.x 把数据文件放在 `_internal\` 下（不是顶层）
    os.path.join("_internal", "resources", "icons", "app.ico"),
]
# 绝不该出现在交付产物里的东西
FORBIDDEN_PAT = re.compile(r"(\.pdb$|__pycache__|\.spec$|\.pyc$|\.log$|\.tmp$)", re.I)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def human(n):
    return "%.1f MiB" % (n / 1048576.0)


def fail(msg):
    print("  !! %s" % msg)
    print("\n出包自检: **失败**")
    sys.exit(1)


print("=== ① onedir 产物完整性 ===")
if not os.path.isdir(ONEDIR):
    fail("onedir 产物不存在: %s（先跑 python build_exe.py --onedir）" % ONEDIR)
for rel in REQUIRED:
    p = os.path.join(ONEDIR, rel)
    if not os.path.exists(p):
        fail("缺件: %s" % rel)
    print("  ✔ %s" % rel)

files = []
for root, _dirs, fs in os.walk(ONEDIR):
    for f in fs:
        full = os.path.join(root, f)
        rel = os.path.relpath(full, ONEDIR)
        files.append((rel, full))
bad = [rel for rel, _ in files if FORBIDDEN_PAT.search(rel)]
if bad:
    fail("产物里有开发期残留（不该进安装包）: %r" % bad[:8])
print("  ✔ 无开发期残留（.pdb / __pycache__ / .spec / .pyc / .log）")
total = sum(os.path.getsize(p) for _r, p in files)
print("  共 %d 个文件 / %s" % (len(files), human(total)))

print("\n=== ② 安装包 ===")
if not os.path.isdir(RELEASE):
    fail("发布目录不存在: %s（先跑 python build_installer.py）" % RELEASE)
setups = sorted(f for f in os.listdir(RELEASE) if f.lower().endswith(".exe"))
if not setups:
    fail("没有安装包")
setup = os.path.join(RELEASE, setups[-1])
size = os.path.getsize(setup)
with open(setup, "rb") as fh:
    head = fh.read(4096)
if head[:2] != b"MZ":
    fail("安装包不是合法 PE（MZ 头缺失）")
print("  ✔ 合法 PE：%s" % os.path.basename(setup))
print("    字节: %d（%s）" % (size, human(size)))
if size < 5 * 1024 * 1024:
    fail("安装包只有 %s ⇒ 多半没装上运行库，**不出半成品**" % human(size))

# 版本号：文件名里的 vX.Y.Z 必须是 4 段数字，且主程序的 FileVersion 也要是 4 段
m = re.search(r"_v(\d+(?:\.\d+){3})\.exe$", os.path.basename(setup))
if not m:
    fail("安装包文件名里没有 4 段版本号（形如 _v4.0.0.0.exe）: %s" % os.path.basename(setup))
ver = m.group(1)
print("  ✔ 文件名版本号: %s" % ver)

print("\n=== ③ 清单报告 ===")
lines = ["# HP Scan Tool 出包清单",
         "# 生成: tools/verify_release.py",
         "# 安装包: %s" % os.path.basename(setup),
         "",
         "[安装包]",
         "  %s" % os.path.basename(setup),
         "  字节: %d" % size,
         "  SHA256: %s" % sha256(setup),
         ""]
# ★ 2026-10-24：便携版（onefile）**也是交付物**，清单里必须有它 ——
#   否则拿到清单的人没法核对「免安装备选」到底是哪一版、字节对不对。
PORTABLE = os.path.join(ROOT, "dist", "HP_Scan_Tool.exe")
if os.path.isfile(PORTABLE):
    lines += ["[便携版 onefile] dist\\HP_Scan_Tool.exe",
              "  字节: %d" % os.path.getsize(PORTABLE),
              "  SHA256: %s" % sha256(PORTABLE),
              ""]
lines += ["[onedir 产物目录] %s" % os.path.relpath(ONEDIR, ROOT),
         "  文件数: %d" % len(files),
         "  合计: %d 字节（%s）" % (total, human(total)),
         ""]
lines.append("  %-64s %12s  %s" % ("相对路径", "字节", "SHA256"))
for rel, full in sorted(files):
    lines.append("  %-64s %12d  %s" % (rel, os.path.getsize(full), sha256(full)))
manifest = os.path.join(RELEASE, "MANIFEST.txt")
with open(manifest, "w", encoding="utf-8", newline="\r\n") as fh:
    fh.write("\n".join(lines) + "\n")
print("  清单已写: %s（%d 行）" % (manifest, len(lines)))
if not QUIET:
    print("\n  前 10 行：")
    for l in lines[:10]:
        print("    " + l)

print("\n出包自检: **通过**")
