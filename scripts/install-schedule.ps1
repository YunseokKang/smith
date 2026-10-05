# Register (or re-register) the Smith scheduled task on Windows: `smith report run-due` every 15 minutes.
# run-due itself decides whether a report is due, prepares research, and answers e-mail questions,
# so running it often is cheap. Re-running this script replaces the task with the same definition.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -Remove
#
# Check afterwards with `smith doctor`.
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$TaskPath = "\Smith\"
$TaskName = "ReportDue"

if ($Remove) {
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
    Write-Output "Removed $TaskPath$TaskName."
    exit 0
}

# pythonw: no console window every 15 minutes. Output goes to data\report-runs.log instead.
$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Db = Join-Path $Root "data\smith.db"
$Config = Join-Path $Root "config\smith.local.toml"
foreach ($Path in @($Python, $Config)) {
    if (-not (Test-Path $Path)) { throw "Missing $Path (create the .venv and the local config first)." }
}

$Action = New-ScheduledTaskAction -Execute $Python -WorkingDirectory $Root `
    -Argument "-m smith report run-due --db `"$Db`" --config `"$Config`""
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
# Laptop-friendly: run on battery, keep running when unplugged; never two runs at once (the ledger also
# fences sends); a run is cut off after 2 hours (research and narrative are bounded well below that).
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Smith: scheduled reports and e-mail answers (read-only adviser)" -Force | Out-Null
Write-Output "Registered $TaskPath$TaskName (every 15 minutes) for $Root."
