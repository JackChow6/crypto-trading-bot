@echo off
chcp 65001 >nul
title V43 Engine - Live Log
cd /d "%~dp0"
echo ================================================================
echo   V43 Trading Engine  -  LIVE LOG  (engine.log)
echo ================================================================
echo.
powershell -NoProfile -Command "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Content -Path 'v43\engine.log' -Wait -Tail 60 -Encoding utf8"
pause
