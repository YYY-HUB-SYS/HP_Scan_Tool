r"""
PyInstaller 打包脚本 — HP Scan Tool v4.0 (PySide6)

两种发行形态（默认 onefile，加 --onedir 切目录发布）：

  1) **onefile 便携版**（默认）：`dist/HP_Scan_Tool.exe` 单文件，拷到哪都能跑。
     ⚠️ 代价（实测，2026-10-21）：onefile 每次启动都要把 138.5 MiB 解压到
     `%TEMP%\_MEIxxxxxx` ⇒ ① 启动慢 3.3 秒 ② 崩溃/强杀会**残留** 138.5 MiB/次
     （进程死后能删）③ 别人"清空 %TEMP%"会**打断正在运行的程序**（DLL 被抽走）。

  2) **onedir 安装版**（`--onedir`）：`build/onedir/HP_Scan_Tool/` 目录，
     交给 Inno Setup 打成安装包（见 `installer/hp_scan_tool.iss`）⇒
     **安装后是真实文件，完全不解压 %TEMP%**，走标准安装/卸载流程。
     ★ 这是「发给每个人」时的**正式部署形态**；onefile 版保留给免安装场景。

运行:
    python build_exe.py            # onefile 便携版
    python build_exe.py --onedir   # onedir 目录发布（安装包的输入）
"""

import os
import subprocess
import sys
import tempfile

# Windows 控制台默认编码可能无法输出中文/emoji，强制使用 UTF-8
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")

# ★ onedir 模式的输出目录（**不放在 dist/**）——
#   ① `dist/` 里已有交付用的 `HP_Scan_Tool.exe`（单文件版），
#      onedir 会产出同名**目录** `dist\HP_Scan_Tool\` ⇒ 两者并存极易误取；
#   ② onedir 只是安装包的**中间产物** ⇒ 放 build/ 下，与 PyInstaller 的
#      默认 workpath 同级，语义清楚。
ONEDIR_OUT = os.path.join(HERE, "build", "onedir")

# ── EXE 元数据：写进 Windows 版本资源（文件属性 → Version 标签页）─────────
APP_VERSION = "4.0.0.0"
PRODUCT_NAME = "HP Scan Tool"
COMPANY_NAME = "HP Scan Tool"
FILE_DESCRIPTION = "HP Scan Tool 惠普集成扫描工具 v4.0"
ORIGINAL_FILENAME = "HP_Scan_Tool.exe"

# 图标：按 __file__ 解析成绝对路径 —— 打包脚本可能在任意工作目录下运行
ICON_PATH = os.path.join(HERE, "resources", "icons", "app.ico")

# ── 排除清单 ──────────────────────────────────────────────────────────────
# numpy 并不在 requirements.txt 里：它只是"纠偏选型实测"时 deskew 拖进来的
# 残留，生产代码零 import（exposure.py / skew_detect.py 都是纯 PIL 实现）。
# 但 Pillow 10+ 的 PIL/_typing.py 里有：
#     if TYPE_CHECKING:
#         import numpy.typing as npt
# PyInstaller 的静态分析不看 TYPE_CHECKING，会顺着这条线把整个 numpy 连同
# numpy.libs 里的 OpenBLAS DLL（约 9 MB）一并打进 EXE。运行期 TYPE_CHECKING
# 恒为 False，Pillow 从不真的 import numpy ⇒ 排除安全、且不影响任何功能。
# 实际排除项写在下面的 cmd 列表里（"--exclude-module"）。

# 确保 PyInstaller 已安装
try:
    import PyInstaller  # noqa: F401
except ImportError:
    print("安装 PyInstaller...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])


def write_version_file(directory):
    """生成 PyInstaller 文本格式的版本资源文件，返回其路径。"""
    path = os.path.join(directory, "version_info.txt")
    vers = ", ".join(APP_VERSION.split("."))
    content = (
        "# -*- coding: utf-8 -*-\n"
        "# HP Scan Tool v" + APP_VERSION + " — Windows 版本资源（由 build_exe.py 现场生成）\n"
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        "    filevers=(" + vers + "),\n"
        "    prodvers=(" + vers + "),\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo(\n"
        "      [\n"
        "        StringTable(\n"
        "          u'040904B0',\n"
        "          [StringStruct(u'CompanyName', u'" + COMPANY_NAME + "'),\n"
        "           StringStruct(u'FileDescription', u'" + FILE_DESCRIPTION + "'),\n"
        "           StringStruct(u'FileVersion', u'" + APP_VERSION + "'),\n"
        "           StringStruct(u'InternalName', u'HP_Scan_Tool'),\n"
        "           StringStruct(u'LegalCopyright', u'Copyright (C) 2026 HP Scan Tool Project'),\n"
        "           StringStruct(u'OriginalFilename', u'" + ORIGINAL_FILENAME + "'),\n"
        "           StringStruct(u'ProductName', u'" + PRODUCT_NAME + "'),\n"
        "           StringStruct(u'ProductVersion', u'" + APP_VERSION + "')])\n"
        "      ]),\n"
        "    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    )
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return path


print("=" * 50)
print("  惠普集成扫描工具 v4.0 — 打包中")
print("=" * 50)

# ★ 发行形态：默认 onefile（单文件便携版）；`--onedir` 走目录发布（安装包的输入）
ONEDIR = "--onedir" in sys.argv
MODE_NAME = "onedir（目录发布，供 Inno Setup 打安装包）" if ONEDIR else "onefile（单文件便携版）"
print("  发行形态: %s" % MODE_NAME)

with tempfile.TemporaryDirectory(prefix="hp_scan_tool_version_") as version_dir:
    version_file = write_version_file(version_dir)
    print(f"版本资源文件: {version_file}")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--windowed",
        "--name", "HP_Scan_Tool",
        # 资源文件
        # ★ **必须用绝对路径**：PyInstaller 6.x 里 `--add-data` 的相对路径是相对
        #   **spec 文件所在目录**解析的，而 onedir 模式把 --specpath 指到
        #   build/onedir ⇒ 相对路径会变成 build\onedir\resources（找不到）。
        #   （onefile 模式没这问题：specpath 默认在项目根。）
        "--add-data", f"{os.path.join(HERE, 'resources')}{os.pathsep}resources",
        # EXE 图标（绝对路径，基于 __file__ 解析，不依赖 CWD）
        "--icon", ICON_PATH,
        # EXE 版本资源（Windows 文件属性里的 Version）
        "--version-file", version_file,
        # 项目内模块（确保打包包含）
        "--hidden-import", "escl_engine",
        "--hidden-import", "wia_engine",
        "--hidden-import", "wsd_engine",
        "--hidden-import", "listen_engine",
        "--hidden-import", "scan_coordinator",
        "--hidden-import", "save_manager",
        "--hidden-import", "cache_manager",
        "--hidden-import", "history_manager",
        "--hidden-import", "profile_manager",
        # 命名形态的统一实现（零第三方依赖，只用 uuid + datetime）
        "--hidden-import", "naming",
        # 第三方依赖
        "--hidden-import", "requests",
        "--hidden-import", "urllib3",
        "--hidden-import", "zeroconf",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "xml.etree.ElementTree",
        # 排除：生产代码零 import，却会被 Pillow 的 TYPE_CHECKING 分支顺带拖进来
        "--exclude-module", "numpy",
        # 清理
        "--clean",
        "--noconfirm",
    ]

    if ONEDIR:
        # ★ onedir：**不加 --onefile** ⇒ PyInstaller 默认产出目录
        #   （真实文件，不是运行期解压 ⇒ 不碰 %TEMP%）
        os.makedirs(ONEDIR_OUT, exist_ok=True)
        cmd += [
            "--distpath", ONEDIR_OUT,
            "--workpath", os.path.join(ONEDIR_OUT, "_work"),
            "--specpath", ONEDIR_OUT,
        ]
    else:
        cmd.append("--onefile")

    cmd.append(os.path.join(HERE, "hp_scan_gui_pyside.py"))

    print(f"\n命令: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=HERE)

if result.returncode == 0:
    if ONEDIR:
        app_dir = os.path.join(ONEDIR_OUT, "HP_Scan_Tool")
        exe = os.path.join(app_dir, "HP_Scan_Tool.exe")
        total = 0
        count = 0
        for root, _dirs, files in os.walk(app_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                    count += 1
                except OSError:
                    pass
        print(f"\n打包成功（onedir）!")
        print(f"  目录: {app_dir}")
        print(f"  主程序: {exe}（{os.path.getsize(exe) / (1024 * 1024):.1f} MB）")
        print(f"  整个目录: {count} 个文件 / {total / (1024 * 1024):.1f} MiB")
        print(f"  ⇒ 交给 installer/hp_scan_tool.iss 打安装包")
    else:
        exe = os.path.join(DIST, "HP_Scan_Tool.exe")
        size_mb = os.path.getsize(exe) / (1024 * 1024) if os.path.exists(exe) else 0
        print(f"\n打包成功!")
        print(f"  位置: {exe}")
        print(f"  大小: {size_mb:.1f} MB")
else:
    print(f"\n打包失败，返回码: {result.returncode}")
    sys.exit(1)
