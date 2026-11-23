# Compile and run the pure Reducer/Policy host tests (no Arduino, no hardware).
# Uses the zig C++ compiler from .venv (pip install -r firmware/xiao_esp32s3/test/host/requirements.txt).
# Also checks that host/ad3_bridge/demo_scenarios.json matches the current policy;
# pass -UpdateScenarios to regenerate it after an intentional policy change.
param([switch]$UpdateScenarios, [string]$Python = '')
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $repoRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Prepare .venv and install firmware/xiao_esp32s3/test/host/requirements.txt (ziglang).'
}
$work = Join-Path $repoRoot 'captures\test_tmp'
New-Item -ItemType Directory -Force $work | Out-Null
$exe = Join-Path $work 'test_observation_core.exe'
$generated = Join-Path $work 'demo_scenarios.json'
$committed = Join-Path $repoRoot 'host\ad3_bridge\demo_scenarios.json'

# libc++ headers in zig emit nullability warnings unrelated to this code.
& $Python -m ziglang c++ -std=c++17 -Wall -Wextra -Werror -Wno-nullability-completeness `
    -I (Join-Path $repoRoot 'firmware\xiao_esp32s3\include') `
    (Join-Path $repoRoot 'firmware\xiao_esp32s3\test\host\test_observation_core.cpp') -o $exe 2>&1 |
    Where-Object { "$_" -notmatch 'libcxx|nullability|^\s*\d* \||^\s*\||^\s*$|warnings? generated' }
if ($LASTEXITCODE -ne 0) { Write-Host 'Core test build FAILED'; exit 1 }

& $exe --scenarios $generated
if ($LASTEXITCODE -ne 0) { exit 1 }

# PR3a DeviceScheduler with mock servo/camera (host only).
$schedExe = Join-Path $work 'test_observation_scheduler.exe'
& $Python -m ziglang c++ -std=c++17 -Wall -Wextra -Werror -Wno-nullability-completeness `
    -I (Join-Path $repoRoot 'firmware\xiao_esp32s3\include') `
    (Join-Path $repoRoot 'firmware\xiao_esp32s3\test\host\test_observation_scheduler.cpp') -o $schedExe 2>&1 |
    Where-Object { "$_" -notmatch 'libcxx|nullability|^\s*\d* \||^\s*\||^\s*$|warnings? generated' }
if ($LASTEXITCODE -ne 0) { Write-Host 'Scheduler test build FAILED'; exit 1 }
& $schedExe
if ($LASTEXITCODE -ne 0) { exit 1 }

# Low-frequency forest core (reference only; not called by the firmware yet).
$lffExe = Join-Path $work 'test_lowfreq_forest_core.exe'
& $Python -m ziglang c++ -std=c++17 -Wall -Wextra -Werror -Wno-nullability-completeness `
    -I (Join-Path $repoRoot 'firmware\xiao_esp32s3\include') `
    (Join-Path $repoRoot 'firmware\xiao_esp32s3\test\host\test_lowfreq_forest_core.cpp') -o $lffExe 2>&1 |
    Where-Object { "$_" -notmatch 'libcxx|nullability|^\s*\d* \||^\s*\||^\s*$|warnings? generated' }
if ($LASTEXITCODE -ne 0) { Write-Host 'Low-frequency forest core build FAILED'; exit 1 }
& $lffExe
if ($LASTEXITCODE -ne 0) { exit 1 }

if ($UpdateScenarios) {
    Copy-Item -LiteralPath $generated -Destination $committed -Force
    Write-Host "Updated $committed"
} elseif (-not (Test-Path -LiteralPath $committed) -or
          (Get-FileHash $generated).Hash -ne (Get-FileHash $committed).Hash) {
    Write-Host 'FAILED demo_scenarios.json differs from the current policy; rerun with -UpdateScenarios after review.'
    exit 1
} else {
    Write-Host 'OK demo_scenarios.json matches the current policy'
}
exit 0
