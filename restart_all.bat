@echo off
title GMGN Agent - Clean Restart (kills stuck processes first)
cd /d "%~dp0"

echo ============================================================
echo  Stopping any existing GMGN screener + dashboard processes
echo  (kills the whole tree so no orphaned python survives)...
echo ============================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0kill_gmgn.ps1"

echo.
echo Waiting 2s for processes/ports to settle...
timeout /t 2 /nobreak >nul

echo ============================================================
echo  Starting the Gentleman screener and dashboard fresh,
echo  each in its OWN window (one screener, one dashboard)...
echo ============================================================
start "GMGN Screener" cmd /k start_screener.bat
start "GMGN Terminal Dashboard" cmd /k start_tui.bat

echo.
echo  Done. Both started.
echo  - Close the Screener/Dashboard windows individually to stop each.
echo  - Be careful: only ONE dashboard window should be running,
echo    otherwise the dash flickers back.
echo.
pause
