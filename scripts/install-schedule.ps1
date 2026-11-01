# Register (or re-register) the Smith scheduled tasks on Windows:
#   Smith\ReportDue   every 15 minutes: `smith report run-due` (reports, research, event notes, every mail answer)
#   Smith\CounselDue  every -CounselMinutes (default 1): `smith mail answer --counsel-only --log` (the client's [상담]
#                     mails and replies in recent counsel threads only: one Gmail search and a few reads, FR-49)
# run-due itself decides whether a report is due, prepares research, and answers e-mail questions, so running it often
# is cheap; the counsel routine reads nothing else, so running it every minute is cheap too. Re-running this script
# replaces both tasks with the same definitions.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -CounselMinutes 2
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -NoCounsel     # ReportDue only
#   powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -Remove
#
# Check afterwards with `smith doctor`.
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [int]$CounselMinutes = 1,
    [switch]$NoCounsel,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$TaskPath = "\Smith\"
$TaskName = "ReportDue"
$CounselTaskName = "CounselDue"

if ($Remove) {
    foreach ($Name in @($TaskName, $CounselTaskName)) {
        if (Get-ScheduledTask -TaskPath $TaskPath -TaskName $Name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $Name -Confirm:$false
            Write-Output "Removed $TaskPath$Name."
        }
    }
    exit 0
}
if ($CounselMinutes -lt 1 -or $CounselMinutes -gt 15) { throw "-CounselMinutes must be 1 to 15." }

# pythonw: no console window every few minutes. Output goes to data\report-runs.log instead.
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

if ($NoCounsel) {
    if (Get-ScheduledTask -TaskPath $TaskPath -TaskName $CounselTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $CounselTaskName -Confirm:$false
        Write-Output "Removed $TaskPath$CounselTaskName."
    }
    exit 0
}
# The counsel routine: a question is claimed atomically in the ledger, so overlapping runs (this task's own, or
# ReportDue's) never answer the same mail twice. Instances run in parallel on purpose: an answer takes minutes, and a
# second question sent right after the first should not wait for it (user report 2026-11-01). A tick that finds
# nothing new exits in seconds; the daily answer limit bounds how many answers can run at once.
$CounselAction = New-ScheduledTaskAction -Execute $Python -WorkingDirectory $Root `
    -Argument "-m smith mail answer --counsel-only --log --db `"$Db`" --config `"$Config`""
$CounselTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes $CounselMinutes)
$CounselSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances Parallel -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskPath $TaskPath -TaskName $CounselTaskName -Action $CounselAction -Trigger $CounselTrigger `
    -Settings $CounselSettings -Description "Smith: counsel mails ([상담]) answered within minutes (read-only adviser)" -Force | Out-Null
Write-Output "Registered $TaskPath$CounselTaskName (every $CounselMinutes minute(s)) for $Root."
