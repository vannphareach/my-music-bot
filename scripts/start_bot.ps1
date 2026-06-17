$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found at $pythonExe"
}

$logsDir = Join-Path $projectRoot "logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
}

$stdoutLog = Join-Path $logsDir "bot-stdout.log"
$stderrLog = Join-Path $logsDir "bot-stderr.log"

& $pythonExe "bot.py" 1>> $stdoutLog 2>> $stderrLog
