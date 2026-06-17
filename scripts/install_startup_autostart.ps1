$ErrorActionPreference = "Stop"

$startupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$source = "E:\VSCode\start_mydmb_bot.cmd"
$target = Join-Path $startupDir "start_mydmb_bot.cmd"

if (-not (Test-Path $source)) {
    throw "Launcher not found: $source"
}

if (-not (Test-Path $startupDir)) {
    New-Item -ItemType Directory -Path $startupDir | Out-Null
}

Copy-Item $source $target -Force
Write-Host "Installed startup autostart launcher at: $target"
