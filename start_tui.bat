@echo off
title GMGN Agent - Terminal Dashboard
cd /d "%~dp0"
python tui.py
echo.
echo ===============================================
echo  Dashboard stopped. Press any key to close this window.
echo ===============================================
pause >nul
