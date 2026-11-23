# Run every automated check in the repository and print one summary table.
# Hardware is never touched: no upload, no serial port, no Flash access.
param(
    [switch]$SkipFirmware,        # skip the three PlatformIO builds (~30 s)
    [switch]$SkipAnalysis,        # skip analysis/passive_detection tests
    [string]$AnalysisPython = '', # Python with numpy/scipy/pandas/matplotlib
    [string]$BridgePython = ''   # Python with host/ad3_bridge/requirements-test.txt
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pioPython = Join-Path $env:USERPROFILE '.platformio\penv\Scripts\python.exe'
$results = [System.Collections.Generic.List[object]]::new()

function Add-Result($name, $status, $detail) {
    $results.Add([pscustomobject]@{ Check = $name; Status = $status; Detail = $detail })
}

function Invoke-Check($name, [scriptblock]$command) {
    Write-Host "`n=== $name" -ForegroundColor Cyan
    $output = & $command 2>&1
    $code = $LASTEXITCODE
    $output | ForEach-Object { Write-Host $_ }
    $summary = ($output | ForEach-Object { "$_" } |
        Select-String -Pattern '^(Ran \d+ tests|OK|FAILED|.*\[(SUCCESS|FAILED)\]|[ℹ#] (tests|pass|fail) \d+)' |
        ForEach-Object { $_.Line.Trim(' ', '=') }) -join ' / '
    Add-Result $name ($(if ($code -eq 0) { 'PASS' } else { 'FAIL' })) $summary
}

function Test-AnalysisPython($candidate) {
    if (-not $candidate) { return $false }
    try {
        & $candidate -c 'import numpy, scipy, pandas, matplotlib' 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

Push-Location $repoRoot
try {
    if (-not (Test-Path -LiteralPath $pioPython)) {
        throw 'PlatformIO Python was not found. Install the PlatformIO IDE extension first.'
    }

    # Host tools run on the PlatformIO Python, matching scripts/*.ps1.
    Invoke-Check 'Host: evidence client' {
        & $pioPython -B -m unittest discover -s host/evidence -p 'test_*.py'
    }
    Invoke-Check 'Host: vision/piezo capture' {
        & $pioPython -B -m unittest discover -s host/vision_capture -p 'test_*.py'
    }
    Invoke-Check 'Host: servo bench' {
        & $pioPython -B -m unittest discover -s host/servo_bench -p 'test_*.py'
    }

    $ad3Python = if ($BridgePython) { $BridgePython } else { Join-Path $repoRoot '.venv\Scripts\python.exe' }
    if (Test-Path -LiteralPath $ad3Python) {
        Invoke-Check 'ML: vision dataset contracts/splits' {
            & $ad3Python -B -m unittest discover -s ml/vision -p 'test_*.py'
        }
        # Windows Store Python can virtualize the system Temp folder; keep fixtures local.
        New-Item -ItemType Directory -Force (Join-Path $repoRoot 'captures\test_tmp') | Out-Null
        Invoke-Check 'Host: AD3 bridge' {
            & $ad3Python -B -c "import tempfile,unittest; from pathlib import Path; tempfile.tempdir=str(Path('captures/test_tmp').resolve()); result=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('host/ad3_bridge')); raise SystemExit(not result.wasSuccessful())"
        }
    } else {
        Add-Result 'Host: AD3 bridge' 'FAIL' 'Prepare .venv with host/ad3_bridge/requirements-test.txt or pass -BridgePython'
    }

    if (Get-Command node -ErrorAction SilentlyContinue) {
        $groupNodeVersion = 'v' + (Get-Content (Join-Path $repoRoot 'host/ad3_bridge/.node-version') -Raw).Trim()
        if ((& node --version) -eq $groupNodeVersion) {
            Invoke-Check 'Host: evidence boot groups' {
                # Run node:test in-process; --test spawns a blocked sandbox child.
                node host/ad3_bridge/test_evidence_groups.cjs
            }
            Invoke-Check 'Host: observation view' {
                node host/ad3_bridge/test_observation_view.cjs
            }
            Invoke-Check 'Host: collection annotation UI' {
                node host/vision_capture/test_collection_ui.cjs
            }
        } else {
            Add-Result 'Host: evidence boot groups' 'FAIL' "Use pinned Node.js $groupNodeVersion"
        }
    } else {
        Add-Result 'Host: evidence boot groups' 'FAIL' 'Node.js is required for the evidence display tests.'
    }

    # The analysis pipeline needs scientific packages that PlatformIO Python lacks.
    if ($SkipAnalysis) {
        Add-Result 'Analysis: passive detection' 'SKIP' '-SkipAnalysis'
    } else {
        $python = $null
        foreach ($candidate in @($AnalysisPython, 'python', 'py')) {
            if (Test-AnalysisPython $candidate) { $python = $candidate; break }
        }
        if ($python) {
            Invoke-Check 'Analysis: passive detection' {
                & $python -B -m unittest discover -s analysis/passive_detection -p 'test_*.py'
            }
        } else {
            Add-Result 'Analysis: passive detection' 'FAIL' `
                'No Python with numpy/scipy/pandas/matplotlib. pip install -r analysis/passive_detection/requirements.txt, or pass -AnalysisPython'
        }
    }

    # Pure Reducer/Policy core shared with the firmware; compiled for the host with zig.
    if (Test-Path -LiteralPath $ad3Python) {
        Invoke-Check 'Firmware core: reducer/policy (host)' {
            & (Join-Path $PSScriptRoot 'run_core_tests.ps1') -Python $ad3Python
        }
    } else {
        Add-Result 'Firmware core: reducer/policy (host)' 'FAIL' 'Prepare .venv with firmware/xiao_esp32s3/test/host/requirements.txt'
    }

    if ($SkipFirmware) {
        Add-Result 'Firmware builds' 'SKIP' '-SkipFirmware'
    } else {
        foreach ($env in 'xiao_bringup', 'xiao_vision', 'xiao_evidence', 'nano_evidence') {
            Invoke-Check "Firmware build: $env" {
                & (Join-Path $PSScriptRoot 'pio.ps1') run -d firmware/xiao_esp32s3 -e $env
            }
        }
    }
} finally {
    Pop-Location
}

Write-Host "`n=== Summary" -ForegroundColor Cyan
$results | Format-Table -AutoSize -Wrap | Out-String -Width 200 | Write-Host
$failed = @($results | Where-Object Status -eq 'FAIL').Count
if ($failed) {
    Write-Host "$failed check(s) failed." -ForegroundColor Red
    exit 1
}
Write-Host 'All executed checks passed. Hardware behaviour is NOT verified by this script.' -ForegroundColor Green
exit 0
