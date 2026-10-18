@echo off
rem Qadam AI - first-time setup for Windows (creates .venv and installs dependencies)
setlocal
cd /d "%~dp0"

set PYV=
for %%V in (3.12 3.13 3.11) do (
  if not defined PYV (
    py -%%V -c "import sys" >nul 2>nul && set PYV=%%V
  )
)
if not defined PYV (
  echo.
  echo [ERROR] Python 3.11-3.13 was not found.
  echo Install Python 3.12 from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
  echo or run in PowerShell:  winget install Python.Python.3.12
  echo Then close this window and run setup.bat again.
  pause
  exit /b 1
)

echo Using Python %PYV%
py -%PYV% -m venv .venv
if errorlevel 1 (
  echo [ERROR] Could not create the virtual environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Package installation failed. Check your internet connection and read the messages above.
  pause
  exit /b 1
)

if not exist .env copy .env.example .env >nul

set /p AUDIO="Install speech recognition for Live-Coach (faster-whisper, ~500 MB on first use)? [y/N] "
if /i "%AUDIO%"=="y" pip install -r requirements-audio.txt

echo.
echo Setup finished. Now run run.bat
pause
endlocal
