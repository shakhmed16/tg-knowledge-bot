@echo off
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul
echo Running AI diagnostics, please wait...
".venv\Scripts\python.exe" check_ai.py > check_ai_log.txt 2>&1
type check_ai_log.txt
echo.
echo ---------------------------------------------
echo Full output saved to check_ai_log.txt
echo ---------------------------------------------
pause
