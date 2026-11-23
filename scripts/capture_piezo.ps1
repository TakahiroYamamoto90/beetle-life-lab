$ErrorActionPreference = 'Stop'
$capturePython = Join-Path $env:USERPROFILE '.platformio\penv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $capturePython)) {
    throw 'PlatformIO Python was not found. Use Python with pyserial to run capture_piezo.py.'
}
& $capturePython -B (Join-Path $PSScriptRoot 'capture_piezo.py') @args
exit $LASTEXITCODE
