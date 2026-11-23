@echo off
setlocal
chcp 65001 >nul
"%~dp0python\python.exe" -B -X utf8 "%~dp0launcher.py" start %*
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%
