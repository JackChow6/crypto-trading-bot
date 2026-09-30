@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Starting V43 engine (GAINERS top20)...
start "" /min cmd /c "python.exe -u v43\engine.py --config config.yaml --gainers > v43\engine.log 2>&1"
echo Started. Watch log: double-click 看引擎日志.bat
timeout /t 3 >nul
