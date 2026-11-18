@echo off
rem Double-click this to install everything clip-watcher needs (asks before installing).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set ERR=%ERRORLEVEL%
rem keep the window open when double-clicked from Explorer
echo %CMDCMDLINE% | find /i "%~nx0" >nul && pause
exit /b %ERR%
