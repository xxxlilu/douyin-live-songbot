@echo off
chcp 936 >nul
setlocal EnableExtensions
title 抖音弹幕点歌机

cd /d "%~dp0"

rem ---- 没管理员权限就自己弹 UAC 重新启动 ----
if not defined SONGBOT_NO_ELEVATE (
    net session >nul 2>&1
    if errorlevel 1 (
        echo [信息] 需要管理员权限，正在弹出授权窗口...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
        exit /b
    )
)

echo ============================================================
echo    抖音弹幕点歌  -  网易云「下一首播放」
echo ============================================================
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 找不到虚拟环境：%PY%
    echo        先在项目目录运行 setup.ps1 装依赖。
    pause
    exit /b 1
)
if not exist "%~dp0config.ini" (
    echo [错误] 找不到 config.ini
    pause
    exit /b 1
)

echo [1/3] 检查网易云客户端...
tasklist /FI "IMAGENAME eq cloudmusic.exe" 2>nul | find /I "cloudmusic.exe" >nul
if errorlevel 1 (
    if exist "C:\Program Files\Netease\CloudMusic\cloudmusic.exe" (
        echo       没开着，正在启动...
        start "" "C:\Program Files\Netease\CloudMusic\cloudmusic.exe"
        timeout /t 12 /nobreak >nul
    ) else (
        echo       [警告] 没找到网易云，点歌会失败
    )
) else (
    echo       已运行
)

echo [2/3] 检查配置...
findstr /B /C:"room_url = https://live.douyin.com/000000000" "config.ini" >nul 2>&1
if not errorlevel 1 (
    echo       [错误] config.ini 里的 room_url 还是占位地址
    echo              把它改成你自己的直播间链接再启动。
    pause
    exit /b 1
)
for /f "tokens=2 delims==" %%a in ('findstr /B /C:"room_url" config.ini') do set "ROOM=%%a"
echo       直播间：%ROOM%
echo       Ctrl+C 随时停止
echo.
echo [3/3] 开始监听
echo ------------------------------------------------------------

rem Python 用 UTF-8 输出，切一下代码页，否则中文日志会花
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

"%PY%" -u -m songbot -v run
set "CODE=%errorlevel%"

chcp 936 >nul
echo.
echo ------------------------------------------------------------
if "%CODE%"=="0" (
    echo 程序已退出。
) else (
    echo 程序退出，错误码 %CODE%。日志在 logs\songbot.log
)
echo.
pause
