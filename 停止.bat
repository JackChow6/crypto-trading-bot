@echo off
echo Stopping V43 engine...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*v43*engine.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Stopped.
timeout /t 2 >nul
