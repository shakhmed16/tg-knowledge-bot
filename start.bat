@echo off
cd /d "%~dp0"

echo [1/5] Checking Python...
where py >nul 2>&1
if %errorlevel%==0 (set "PY=py") else (set "PY=python")
%PY% --version
if errorlevel 1 goto nopython

set "VPY=.venv\Scripts\python.exe"

echo [2/5] Checking virtual environment...
if not exist "%VPY%" goto mkvenv
rem A .venv copied from another machine points at a python.exe that is not
rem here: the file exists but will not run. Test it by running it, not by
rem checking that the file is present.
"%VPY%" -c "import sys" >nul 2>&1
if not errorlevel 1 goto venvok
echo   Existing .venv does not work on this machine
echo   (it was created by another user or another Python) - recreating...
rmdir /s /q .venv
if exist ".venv" goto venvlocked

:mkvenv
echo   Creating virtual environment...
%PY% -m venv .venv
if errorlevel 1 goto fail
:venvok

echo [3/5] Installing SOCKS support offline (from wheels folder)...
"%VPY%" -m pip install --isolated --no-index --find-links wheels PySocks --disable-pip-version-check -q
if errorlevel 1 echo   (skipped - wheel not found, continuing)

echo [4/5] Installing dependencies...
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt
if not errorlevel 1 goto run

echo.
echo Install via proxy failed. Retrying with proxy disabled...
set "ALL_PROXY="
set "all_proxy="
set "HTTP_PROXY="
set "http_proxy="
set "HTTPS_PROXY="
set "https_proxy="
set "NO_PROXY=*"
set "no_proxy=*"
"%VPY%" -m pip install --isolated --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail

:run
echo [5/5] Starting bot...
"%VPY%" bot.py
echo.
echo Bot stopped.
pause
exit /b 0

:nopython
echo.
echo ============== ERROR ==============
echo Python not found. Install it from python.org and tick
echo "Add python.exe to PATH" during setup.
echo ===================================
pause
exit /b 1

:venvlocked
echo.
echo ============== ERROR ==============
echo Could not delete the .venv folder - something is using it.
echo Close the running bot and any open editor, then run start.bat again.
echo Or delete the .venv folder manually and rerun.
echo ===================================
pause
exit /b 1

:fail
echo.
echo ============== ERROR ==============
echo See the message above. Troubleshooting: README.md
echo ===================================
pause
exit /b 1
