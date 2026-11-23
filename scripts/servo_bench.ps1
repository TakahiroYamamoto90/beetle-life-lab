# Servo bench console for env:xiao_servo_test. The PlatformIO Python includes pyserial.
# Usage: scripts/servo_bench.ps1 --port COM3 [--sequence small|full|steps|center] [--off-after]
$ErrorActionPreference = 'Stop'
$benchPython = Join-Path $env:USERPROFILE '.platformio\penv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $benchPython)) {
    throw 'PlatformIO Python was not found. Use Python with pyserial to run host/servo_bench/servo_bench.py.'
}
$repoRoot = Split-Path -Parent $PSScriptRoot
& $benchPython -B (Join-Path $repoRoot 'host\servo_bench\servo_bench.py') @args
exit $LASTEXITCODE
