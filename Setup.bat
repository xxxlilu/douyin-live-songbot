@echo off
chcp 936 >nul
setlocal
title 点歌机 - 首次安装
cd /d "%~dp0"

echo.
echo ============================================================
echo   抖音直播间点歌机 · 首次安装
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*

echo.
pause
