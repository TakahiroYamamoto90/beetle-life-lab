$ErrorActionPreference = 'Stop'
$bridgePython = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $bridgePython)) {
    throw 'Prepare .venv and install host/ad3_bridge/requirements.txt first; see README.md (セットアップ).'
}
& $bridgePython -B (Join-Path $PSScriptRoot '..\host\ad3_bridge\server.py') @args
exit $LASTEXITCODE
