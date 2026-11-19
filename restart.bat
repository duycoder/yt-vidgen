@echo off
rem Restart vidgen (e.g. after a code update): stop it, then start it again.
cd /d "%~dp0"
call "%~dp0shutdown.bat" || exit /b 1
%SystemRoot%\System32\timeout.exe /t 1 >nul
call "%~dp0start.bat"
