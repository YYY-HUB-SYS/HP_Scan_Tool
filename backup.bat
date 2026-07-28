@echo off
chcp 65001 >nul
echo ========================================
echo   HP Scan Tool — 下班存档
echo ========================================
echo.

REM 生成时间戳 (格式: YYYY-MM-DD_HH-MM-SS)
for /f "tokens=2 delims==" %%a in ('wmic os get localdatetime /value') do set "dt=%%a"
set "TIMESTAMP=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%_%dt:~8,2%-%dt:~10,2%-%dt:~12,2%"

echo [%date% %time%] 开始存档...
echo.

REM 检查 git 仓库
git status --porcelain > nul
if %errorlevel% neq 0 (
    echo [错误] 当前目录不是 Git 仓库
    pause
    exit /b 1
)

REM 暂存所有更改，但排除虚拟环境目录
echo [1/3] 暂存所有更改...
git add -A
git reset -- .venv/ venv/ env/ 2>nul

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
echo   存档成功！
echo   提交信息: 下班存档 %TIMESTAMP%
echo ========================================
echo.
pause
