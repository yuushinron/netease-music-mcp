@echo off
cd /d "%~dp0"
where cloudflared >nul 2>nul
if errorlevel 1 (
  echo cloudflared is not installed.
  echo Run this once in PowerShell or CMD:
  echo winget install --id Cloudflare.cloudflared -e
  pause
  exit /b 1
)
echo Starting Cloudflare quick tunnel for http://127.0.0.1:8080
cloudflared tunnel --url http://127.0.0.1:8080
