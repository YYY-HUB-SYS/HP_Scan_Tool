@echo off
chcp 65001 >nul
title 惠普集成扫描工具
cd /d "%~dp0"

echo ============================================
echo   惠普集成扫描工具 v3.3
echo   eSCL + WIA 双引擎
echo   适配 HP 7720 / M232 / M1216
echo ============================================
echo.

python --version >nul 2>&1 || (
    echo [错误] 未找到 Python
    pause & exit /b 1
)

python -c "import requests, PIL, zeroconf" >nul 2>&1 || (
    echo [提示] 正在安装依赖...
    pip install requests zeroconf Pillow pywin32
)

echo [启动]
python hp_scan_gui.py
pause
