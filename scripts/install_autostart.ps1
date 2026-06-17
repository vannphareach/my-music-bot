$ErrorActionPreference = "Stop"

$taskName = "MyDMB-DiscordMusicBot"
$projectRoot = Split-Path -Parent $PSScriptRoot
$startScript = Join-Path $PSScriptRoot "start_bot.ps1"

if (-not (Test-Path $startScript)) {
    throw "Missing start script: $startScript"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`""

$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Starts My DMB Discord music bot at user logon" `
    -Force | Out-Null

Write-Host "Installed startup task: $taskName"
Write-Host "Project root: $projectRoot"
Write-Host "Use 'Start-ScheduledTask -TaskName $taskName' to test it now."
