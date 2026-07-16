"""
PyInstaller 快速打包脚本
运行: python build_exe.py
输出: dist/HP_Scan_Tool.exe（单文件便携版）
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")

# 确保 PyInstaller 已安装
try:
    import PyInstaller
except ImportError:
    print("安装 PyInstaller...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])

print("=" * 50)
print("  惠普集成扫描工具 — 打包中")
print("=" * 50)

# 构建命令
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--name", "HP_Scan_Tool",
    "--add-data", f"escl_engine.py{os.pathsep}.",
    "--add-data", f"wia_engine.py{os.pathsep}.",
    "--hidden-import", "requests",
    "--hidden-import", "zeroconf",
    "--hidden-import", "customtkinter",
    "--hidden-import", "PIL",
    "--hidden-import", "xml.etree.ElementTree",
    "--hidden-import", "json",
    "--hidden-import", "threading",
    "--hidden-import", "tkinter",
    "--hidden-import", "tkinter.ttk",
    "--hidden-import", "tkinter.filedialog",
    "--hidden-import", "tkinter.messagebox",
    "--clean",
    "--noconfirm",
    os.path.join(HERE, "hp_scan_gui.py"),
]

print(f"\n命令: {' '.join(cmd)}\n")
result = subprocess.run(cmd, cwd=HERE)

if result.returncode == 0:
    exe = os.path.join(DIST, "HP_Scan_Tool.exe")
    size_mb = os.path.getsize(exe) / (1024 * 1024) if os.path.exists(exe) else 0
    print(f"\n打包成功!")
    print(f"  位置: {exe}")
    print(f"  大小: {size_mb:.1f} MB")
else:
    print(f"\n打包失败，返回码: {result.returncode}")
    sys.exit(1)
