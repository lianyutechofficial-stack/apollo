@echo off
REM ============================================
REM  Apollo Agent 打包脚本 (Windows)
REM  生成 dist\ApolloAgent.exe
REM ============================================

echo ========================================
echo   Apollo Agent Build (Windows)
echo ========================================
echo.

cd /d "%~dp0\.."

REM 安装依赖
echo 安装依赖...
pip install pyinstaller pywebview 2>nul

REM 清理
if exist dist rmdir /s /q dist

REM 打包
echo 开始打包...
pyinstaller build\apollo_agent.spec --distpath dist --workpath build\tmp --clean -y

if exist dist\ApolloAgent.exe (
    echo.
    echo √ 构建完成: dist\ApolloAgent.exe
    for %%A in (dist\ApolloAgent.exe) do echo   大小: %%~zA bytes
) else (
    echo.
    echo × 构建失败
    exit /b 1
)

echo.
echo 用户下载后双击即可运行，无需安装 Python。
pause
