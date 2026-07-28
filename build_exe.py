"""
PyInstaller 打包脚本 — HP Scan Tool v4.0 (PySide6)
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
    import PyInstaller  # noqa: F401
except ImportError:
    print("安装 PyInstaller...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])

print("=" * 50)
print("  惠普集成扫描工具 v4.0 — 打包中")
print("=" * 50)

# 构建命令
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--name", "HP_Scan_Tool",
    # 资源文件
    "--add-data", f"resources{os.pathsep}resources",
    # 项目内模块（确保打包包含）
    "--hidden-import", "escl_engine",
    "--hidden-import", "wia_engine",
    "--hidden-import", "wsd_engine",
    "--hidden-import", "listen_engine",
    "--hidden-import", "scan_coordinator",
    "--hidden-import", "cache_manager",
    "--hidden-import", "history_manager",
    "--hidden-import", "profile_manager",
    # 第三方依赖
    "--hidden-import", "requests",
    "--hidden-import", "urllib3",
    "--hidden-import", "zeroconf",
    "--hidden-import", "PIL",
    "--hidden-import", "PIL.Image",
    "--hidden-import", "xml.etree.ElementTree",
    # 清理
    "--clean",
    "--noconfirm",
    os.path.join(HERE, "hp_scan_gui_pyside.py"),
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
