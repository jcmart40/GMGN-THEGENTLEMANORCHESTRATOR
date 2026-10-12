@echo off
title GMGN Agent - Launcher
cd /d "%~dp0"
echo Starting screener and terminal dashboard, each in its own window...
start "GMGN Screener" cmd /k start_screener.bat
start "GMGN Terminal Dashboard" cmd /k start_tui.bat
echo.
echo Both started. This window can be closed -- it does not control them.
echo Close the individual Screener/Dashboard windows to stop each one.
pause
