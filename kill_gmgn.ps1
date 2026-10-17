# Kill any running GMGN bot/dashboard processes by command-line match so we
# never kill unrelated python.exe processes. Kills the WHOLE process tree
# (parent + any child it forked) -- the handoff warned two tui.py processes
# (a parent forks a child) make flicker worse, and a stuck screener spun at
# ~943s CPU while holding the old rate-limit backoff. Exactly the processes
# this helper cleans so restart_all.bat can start ONE of each.
# Windows PowerShell 5.1 renders non-ASCII as '?' -- force UTF-8 output.

[Console]::OutputEncoding = [Text.UTF8Encoding]::new()

# Match the specific launcher command lines only, using substring search.
# Get-CimInstance handles file path quoting; -like matches ignore case.
$targets = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object {
        ($_.CommandLine -like '*bot.screener*') -or
        ($_.CommandLine -like '*tui.py*')
    }

if ($targets) {
    foreach ($t in $targets) {
        $pid = $t.ProcessId
        $cmd = (($t.CommandLine -replace '\s+', ' ') ?? '')
        Write-Host "Killing GMGN process tree PID $pid : $cmd"
        # taskkill /F /T kills the whole process tree (parent AND children).
        & taskkill.exe /F /T /PID $pid 2>&1 | Out-Host
    }
    Write-Host ("Killed {0} GMGN process tree(s)." -f $targets.Count)
} else {
    Write-Host "No running GMGN screener/dashboard processes found to kill."
}
