# dashboard_reset.ps1
# One double-click switch to: SAVE logs + CLEAR the dashboard for a fresh
# start + optionally PUSH the saved logs to git so you have a recovery copy
# even if credits run out and you can't talk to the assistant.
#
# Safe by design (matches the repo's own "clear the dash" ritual in
# bot/trade_history.py):
#   - PRESERVED forever: paper/live OPEN positions, seen-mint cursors (don't
#     re-buy), AI learning memory, AI Free Will overrides, the MASTER trade
#     history (cap 50000, what the AI review loop reads), and gentleman chat.
#   - ARCHIVED + RESET: the live dashboard display files only (activity log,
#     cleared candidates, dash-bound trade history, AI judgment log, and
#     trade-burst state) -- copied to logs/backups/<timestamp>/ for safety.
#   - Makes backups BEFORE anything is cleared; clears only after a backup
#     was written, so nothing is ever lost.

[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root 'logs'

# ---- File inventory (by role) ----------------------------------------------
$preserveFiles = @(
    'ai_memory.json',          # AI learning memory - never erased
    'ai_overrides.json',       # AI Free Will overrides - never erased
    'seen_mints.json',         # signal-source cursors - don't re-buy
    'trade_history_master.json', # AI review learning history (cap 50000)
    'gentleman_chat.jsonl',    # chat log
    'paper_portfolio.json',    # OPEN paper positions
    'live_positions.json'      # OPEN live positions
)

# Files that the "clear the dash" reset archives & empties.
$resetFiles = @(
    'activity_log.json',       # screening/buy activity
    'cleared_candidates.json', # every buy attempt/outcome
    'trade_history.json',      # dash-bound closed trades (cap 5000)
    'ai_judgment_log.json',    # AI judgment feed
    'trade_burst_state.json'   # trade-burst cooldown timer
)

function Write-Header([string]$msg) { Write-Host "" ; Write-Host "==== $msg ====" ; }

function Backup-Logs([DateTime]$stamp, [string]$label) {
    $dir = Join-Path $logs (Join-Path 'backups' (($label) + '_' + $stamp.ToString('yyyyMMdd_HHmmss')))
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $copied = 0
    # Back up EVERY log file first (preserve + reset alike) so the user has a
    # full recovery snapshot regardless of which reset mode runs next.
    Get-ChildItem -Path $logs -File | Where-Object { $_.Extension -in '.json','.jsonl' -and $_.Directory.Name -eq 'logs' } | ForEach-Object {
        Copy-Item $_.FullName -Destination (Join-Path $dir $_.Name) -Force
        $copied++
    }
    Write-Host ("Backed up {0} log file(s) to {1}" -f $copied, $dir)
    return $dir
}

function Reset-DashboardFiles([string]$backupDir) {
    $cleared = 0
    foreach ($f in $resetFiles) {
        $path = Join-Path $logs $f
        if (Test-Path $path) {
            # Already copied into $backupDir above. Now empty it back to default.
            $default = '[]'
            if ($f -eq 'trade_burst_state.json') { $default = '{"count":0,"paused_until":0.0,"last_trade_ts":0.0}' }
            Set-Content -Path $path -Value $default -Encoding UTF8 -NoNewline
            Write-Host ("  cleared : {0}" -f $f)
            $cleared++
        } else {
            Write-Host ("  skip (absent) : {0}" -f $f)
        }
    }
    Write-Host ("Dashboard files cleared: {0}" -f $cleared)
}

function Confirm-Push([string]$backupDir) {
    Write-Host ""
    Write-Host "=================================================================="
    Write-Host "  OPTIONAL: commit + push the saved logs to GitHub?"
    Write-Host "  Use this if you're running low on credits and want a recovery"
    Write-Host "  copy you (or the assistant) can pull later without needing to"
    Write-Host "  run here."
    Write-Host "=================================================================="
    $ans = Read-Host "  Push the saved logs to GitHub now? (y/N)"
    if ($ans -notmatch '^[yY]') {
        Write-Host "  Skipping push. Backups are safe in: $backupDir"
        return
    }
    Push-Location $root
    try {
        # Never push .env or real secrets -- .gitignore already excludes .env.
        # The whole logs/ tree is .gitignore'd, so we force-add just this
        # snapshot. We push to a DEDICATED branch (logs-backup), never main,
        # so code history stays clean and log snapshots are recoverable
        # separately without bloating main.
        $branch = 'logs-backup'
        # Make sure the branch exists (create it off current HEAD if first run).
        git rev-parse --verify --quiet $branch
        if ($LASTEXITCODE -ne 0) {
            git checkout -b $branch
            if ($LASTEXITCODE -ne 0) { throw "could not create branch $branch" }
        }
        $rel = Split-Path $backupDir -Leaf
        git add -f "logs/backups/$rel"
        if ($LASTEXITCODE -ne 0) { throw "git add failed (exit $LASTEXITCODE)" }
        $subject = "Log backup " + $rel
        git commit -m $subject
        if ($LASTEXITCODE -eq 0) {
            # First push needs -u; later pushes go to the existing upstream.
            git push -u origin $branch 2>&1
            if ($LASTEXITCODE -ne 0) { git push origin $branch 2>&1 }
            if ($LASTEXITCODE -eq 0) {
                Write-Host ("  Pushed to GitHub branch '{0}'. Recovery copy available remotely." -f $branch)
            } else {
                Write-Host "  Push returned non-zero (may still have gone through). Check git push."
            }
        } else {
            Write-Host "  Nothing to commit (or commit failed). Snapshot still saved locally."
        }
        # Return to main so the working tree isn't left on logs-backup.
        git checkout main 2>&1 | Out-Null
    } catch {
        Write-Host ("  Push skipped: " + $_.Exception.Message)
    } finally {
        Pop-Location
    }
}

# ---- Main menu --------------------------------------------------------------
Write-Header "The Gentleman - fresh dashboard switch"
Write-Host "  This backs up all logs, then can clear the live dashboard for a"
Write-Host "  fresh start. It NEVER erases learning memory, overrides, open"
Write-Host "  positions, seen-mint cursors, or the master trade history."
Write-Host ""
Write-Host "  1) Save logs only (backup, don't clear anything)"
Write-Host "  2) Save + clear dashboard display (standard fresh start)"
Write-Host "  3) Full fresh start (also close all paper/live positions) - careful"
Write-Host "  q) Quit without doing anything"
$choice = Read-Host "  Choose (1/2/3/q)"

if ($choice -match '^[qQ]') { Write-Host "Nothing done. Exiting." ; exit }

if ($choice -match '^[1]') {
    $stamp = Get-Date
    $bk = Backup-Logs $stamp 'save'
    Write-Host "Logs saved. Nothing cleared."
    Confirm-Push $bk
    exit
}

if ($choice -notmatch '^[23]') {
    Write-Host "Invalid choice. Nothing done."
    exit
}

# Pause the bot processes so they can't write into the files mid-reset.
Write-Header "Stopping running screener / dashboard processes"
& (Join-Path $root 'kill_gmgn.ps1')
Start-Sleep -Seconds 1

$stamp = Get-Date
$bk = Backup-Logs $stamp ('reset' + $choice)
Write-Host ""

if ($choice -match '^[2]') {
    Reset-DashboardFiles $bk
    Write-Host ""
    Write-Host "Standard fresh start complete. Restart the bot with restart_all.bat"
}

if ($choice -match '^[3]') {
    Write-Header "FULL fresh start (option 3) - confirming"
    Write-Host "  This will ALSO archive and empty OPEN paper/live positions."
    Write-Host "  Any open trades will be dropped (archived, not destroyed -"
    Write-Host "  you can restore from the backup)."
    $confirm = Read-Host "  Really close ALL positions? (yes/no)"
    if ($confirm -notmatch '^[yY]') {
        Write-Host "Cancelled option 3. Backup was made but nothing else changed."
        Confirm-Push $bk
        exit
    }
    foreach ($f in @('paper_portfolio.json','live_positions.json')) {
        $path = Join-Path $logs $f
        if (Test-Path $path) { Set-Content -Path $path -Value '{"positions":{},"closed":[],"realized_pnl_sol":0.0}' -Encoding UTF8 -NoNewline }
    }
    Reset-DashboardFiles $bk
    Write-Host ""
    Write-Host "Full fresh start complete. Restart manually with restart_all.bat"
}

Confirm-Push $bk
