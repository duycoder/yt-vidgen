@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
rem Already running? just open the browser.
netstat -ano | findstr /r /c:"127.0.0.1:8765 .*LISTENING" >nul
if not errorlevel 1 (
  echo vidgen dang chay - mo trinh duyet.
  start "" http://127.0.0.1:8765
  %SystemRoot%\System32\timeout.exe /t 2 >nul
  exit /b 0
)
start "" http://127.0.0.1:8765
title vidgen server - dong cua so nay de tat
python app.py
