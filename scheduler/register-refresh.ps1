# Registers the Porsche Deal Hunter automatic refresh in Windows Task Scheduler.
#
# This is a PER-USER task (no administrator rights needed). It runs only while
# you are logged in and your computer is on -- Windows cannot refresh listings
# on a machine that is off. Default: every hour.
#
# Usage (in PowerShell, from anywhere):
#   powershell -ExecutionPolicy Bypass -File .\scheduler\register-refresh.ps1
#   powershell -ExecutionPolicy Bypass -File .\scheduler\register-refresh.ps1 -IntervalHours 2
#
param(
    [int]$IntervalHours = 1
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $PSScriptRoot "refresh.bat"
$taskPath = "\PorscheDealHunter\"
$taskName = "Refresh"

if (-not (Test-Path $bat)) { throw "Cannot find $bat" }

$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $repo
# Repeat every N hours, indefinitely, starting a minute from now.
$start   = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
             -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
             -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
             -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
             -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action `
    -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered scheduled task $taskPath$taskName -- runs every $IntervalHours hour(s) while you are logged in."
Write-Host "It appends output to data\refresh.log. Disable it any time with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\scheduler\unregister-refresh.ps1"
Write-Host ""
Write-Host "NOTE: refresh runs ONLY when this computer is on and you are logged in."
Write-Host "It does not monitor while the machine is off or asleep."
