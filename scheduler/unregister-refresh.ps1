# Removes the Porsche Deal Hunter automatic refresh scheduled task.
# Usage: powershell -ExecutionPolicy Bypass -File .\scheduler\unregister-refresh.ps1
$ErrorActionPreference = "SilentlyContinue"
Unregister-ScheduledTask -TaskName "Refresh" -TaskPath "\PorscheDealHunter\" -Confirm:$false
Write-Host "Automatic refresh task removed. (If it was not registered, nothing changed.)"
