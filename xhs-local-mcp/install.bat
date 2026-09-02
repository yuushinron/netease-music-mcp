@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Creating Python virtual environment...
py -3.11 -m venv .venv 2>nul
if errorlevel 1 python -m venv .venv
if errorlevel 1 goto :fail

echo [2/3] Installing Python dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [3/3] Checking cloudflared...
where cloudflared >nul 2>nul
if errorlevel 1 (
  echo cloudflared is not installed yet.
  echo You can install it later with: winget install --id Cloudflare.cloudflared -e
) else (
  echo cloudflared found.
)

echo.
echo Install complete. Next, double-click login.bat once.
pause
exit /b 0

:fail
echo.
echo Install failed. Copy the error above and send it to ChatGPT.
pause
exit /b 1
