@echo off
title GMGN Agent - Screener
cd /d "%~dp0"
python -m bot.screener
echo.
echo ===============================================
echo  Screener stopped. Press any key to close this window.
echo ===============================================
pause >nul
