@echo off
chcp 936 >nul
setlocal EnableExtensions
title 点歌机 - 一键启动（overlay + songbot）

cd /d "%~dp0"

rem 连接器要挂 CEF 桥，必须管理员权限；没有就自己弹 UAC
if not defined N5_NO_ELEVATE (
    net session >nul 2>&1
    if errorlevel 1 (
        echo [信息] 需要管理员权限，正在弹出授权窗口...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
        exit /b
    )
)

set "PY=%~dp0songbot\.venv\Scripts\python.exe"
if not exist "%PY%" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [错误] 找不到 python，也找不到 songbot\.venv
        echo        先跑一次 songbot\setup.ps1
        pause
        exit /b 1
    )
    set "PY=python"
)

chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem overlay 会自己把 songbot 拉起来（overlay\config.json 里的 songbot.auto_start）
cd /d "%~dp0overlay"
"%PY%" -u overlay.py

set "CODE=%errorlevel%"
chcp 936 >nul
echo.
if not "%CODE%"=="0" echo 程序退出，错误码 %CODE%
pause
