@echo off
chcp 65001 >nul
echo ========================================
echo   HP Scan Tool — 下班存档
echo ========================================
echo.

REM 获取当前时间戳
for /f "tokens=1-5 delims=:.," %%a in ("%date% %time%") do (
    set "TIMESTAMP=%%a-%%b-%%c_%%d-%%e"
)

REM 清理时间戳中的空格
set "TIMESTAMP=%TIMESTAMP: =0%"

echo [%date% %time%] 开始存档...
echo.

REM 检查是否有更改需要提交
git status --porcelain > nul
if %errorlevel% neq 0 (
    echo [错误] 当前目录不是 Git 仓库
    pause
    exit /b 1
)

REM 暂存所有更改（包括未跟踪文件）
echo [1/3] 暂存所有更改...
git add -A
if %errorlevel% neq 0 (
    echo [错误] 暂存失败
    pause
    exit /b 1
)

REM 检查是否有内容需要提交
git diff --cached --quiet
if %errorlevel% equ 0 (
    echo [提示] 没有需要存档的更改
    pause
    exit /b 0
)

REM 提交
echo [2/3] 创建存档提交...
git commit -m "下班存档 %TIMESTAMP%"
if %errorlevel% neq 0 (
    echo [错误] 提交失败
    pause
    exit /b 1
)

echo.
echo ========================================
echo   ✅ 存档成功！
echo   提交信息: 下班存档 %TIMESTAMP%
echo ========================================
echo.
pause
