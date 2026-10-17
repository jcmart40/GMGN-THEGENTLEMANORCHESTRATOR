@echo off
title The Gentleman - Fresh Dashboard Switch
cd /d "%~dp0"

echo ============================================================
echo  The Gentleman - fresh dashboard switch
echo  - Backs up ALL logs first (nothing is ever lost instantly)
echo  - Then optionally clears the live dashboard for a fresh start
echo  - Never erases learning memory, overrides, open positions,
echo    seen-mint cursors, or the master trade history
echo  - Can optionally push the saved logs to GitHub as a recovery
echo    copy in case credits run out
echo ============================================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard_reset.ps1"

echo.
echo ============================================================
echo  Done. Backups are in the logs\backups\ folder.
echo  Restart the bot with restart_all.bat when ready.
echo ============================================================
pause
