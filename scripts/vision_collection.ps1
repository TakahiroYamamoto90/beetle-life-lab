$ErrorActionPreference = 'Stop'
$collectionPython = Join-Path $env:USERPROFILE '.platformio\penv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $collectionPython)) {
    throw 'PlatformIO Python was not found.'
}
& $collectionPython -B (Join-Path $PSScriptRoot '..\host\vision_capture\server.py') @args
exit $LASTEXITCODE
