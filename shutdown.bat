@echo off
rem Stop the vidgen server (whoever started it). Warns first if a render / voice job is running.
setlocal
set PID=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"127.0.0.1:8765 .*LISTENING"') do set PID=%%p
if "%PID%"=="" (
  echo vidgen khong chay.
  %SystemRoot%\System32\timeout.exe /t 2 >nul
  exit /b 0
)
curl -s http://127.0.0.1:8765/api/status | findstr /c:"\"busy\":true" >nul
if not errorlevel 1 (
  echo.
  echo  CANH BAO: vidgen dang chay mot viec ^(render / tao giong / tao anh^).
  echo  Tat bay gio se lam hong viec do.
  echo.
  choice /c YN /m "Van tat?"
  if errorlevel 2 exit /b 1
)
taskkill /PID %PID% /F >nul
echo Da tat vidgen ^(PID %PID%^).
%SystemRoot%\System32\timeout.exe /t 2 >nul
