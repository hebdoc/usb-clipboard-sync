@echo off
chcp 65001 >nul
title USB 剪贴板双向同步工具 (PC ^<--^> Android)

echo =======================================================
echo          USB 剪贴板双向同步 (PC ^<--^> Android)
echo =======================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python 环境，请先安装 Python 3 并添加到系统环境变量。
    echo.
    pause
    exit /b 1
)

where adb >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未检测到 ADB 工具，请确保已安装 Android SDK Platform-Tools 并配置环境变量。
    echo.
    pause
    exit /b 1
)

echo [*] 正在启动 USB 双向剪贴板同步服务...
echo [*] 请确保安卓手机已开启【USB 调试】并已通过数据线连接电脑。
echo.

python "%~dp0usb_clipboard_sync.py"

if %errorlevel% neq 0 (
    echo.
    echo [提示] 程序已异常退出。
    pause
)
