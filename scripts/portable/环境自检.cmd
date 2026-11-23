@echo off
setlocal
chcp 65001 >nul
"%~dp0python\python.exe" -B -X utf8 "%~dp0launcher.py" check
set "result=%errorlevel%"
pause
exit /b %result%
