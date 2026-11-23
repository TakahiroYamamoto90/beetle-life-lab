# The existing PlatformIO Python includes pyserial; forward all CLI arguments.
$ErrorActionPreference = 'Stop'
$capturePython = Join-Path $env:USERPROFILE '.platformio\penv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $capturePython)) {
    throw 'PlatformIO Python was not found. Use Python with pyserial to run capture_vision.py.'
}
& $capturePython -B (Join-Path $PSScriptRoot 'capture_vision.py') @args
exit $LASTEXITCODE
