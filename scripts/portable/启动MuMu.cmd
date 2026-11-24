@echo off
setlocal
chcp 65001 >nul
for /d %%D in ("%~dp0*") do if exist "%%~fD\launcher.py" (
    "%%~fD\python\python.exe" -B -X utf8 "%%~fD\launcher.py" start --combined %*
    if errorlevel 1 pause
    exit /b
)
echo Missing MuMu recorder. Extract the complete ZIP to a new folder.
pause
exit /b 1
