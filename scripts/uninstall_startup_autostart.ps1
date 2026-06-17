$ErrorActionPreference = "Stop"

$target = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\start_mydmb_bot.cmd"

if (Test-Path $target) {
    Remove-Item $target -Force
    Write-Host "Removed startup autostart launcher: $target"
} else {
    Write-Host "Startup autostart launcher is already absent."
}
