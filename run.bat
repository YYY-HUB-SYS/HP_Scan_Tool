@echo off
chcp 65001 >nul
title 惠普集成扫描工具 v3.3
cd /d "%~dp0"

echo ============================================
echo   惠普集成扫描工具 v3.3
echo   eSCL + WIA + WSD 三引擎
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

REM CLI 模式：如果第一个参数是 scan/discover/status/history，走 CLI
if "%1"=="scan" goto cli
if "%1"=="discover" goto cli
if "%1"=="status" goto cli
if "%1"=="history" goto cli

REM GUI 模式
echo [启动] GUI 模式
python hp_scan_gui.py
pause
exit /b 0

:cli
echo [启动] CLI 模式: %*
python cli.py %*
pause
exit /b 0
