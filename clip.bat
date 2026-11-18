@echo off
rem Usage: clip <channel> [options]    e.g.  clip summit1g --vertical --burn
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo First run - setting things up...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
    if errorlevel 1 exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0clip_watcher.py" %*
