@echo off
rem Qadam AI - start the app
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
  echo [ERROR] Environment not found. Run setup.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo Starting Qadam AI... the browser will open at http://localhost:8501
streamlit run app.py
if errorlevel 1 (
  echo.
  echo [ERROR] The app stopped with an error. Read the messages above.
  pause
)
