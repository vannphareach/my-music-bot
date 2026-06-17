$ErrorActionPreference = "Stop"

$taskName = "MyDMB-DiscordMusicBot"

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
    Write-Host "Task '$taskName' is not installed."
    exit 0
}

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
Write-Host "Removed startup task: $taskName"
