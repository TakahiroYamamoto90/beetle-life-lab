# Use the existing PlatformIO installation while keeping writable state in this repo.
# Keep raw arguments: PowerShell common parameters would consume PIO's -d / -v.
$pioArguments = $args
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$installedCore = Join-Path $env:USERPROFILE '.platformio'
# The package path appears ~200 times in each compile command (Windows limit 32,767 chars):
# with a long clone path, point BLL_PIO_CORE_DIR at a short folder such as C:\bllpio.
$localCore = if ($env:BLL_PIO_CORE_DIR) { $env:BLL_PIO_CORE_DIR } else { Join-Path $repoRoot '.platformio-local' }
$pioExe = Join-Path $installedCore 'penv\Scripts\platformio.exe'
if (-not (Test-Path -LiteralPath $pioExe)) {
    throw 'PlatformIO Core was not found. Install the PlatformIO IDE extension and allow its initial setup to finish.'
}
# Package managers lock beside their package directories, so use local copies.
$requiredPackages = @('framework-arduinoespressif32', 'toolchain-xtensa-esp32s3',
    'toolchain-riscv32-esp', 'tool-esptoolpy', 'tool-openocd-esp32', 'tool-scons',
    'tool-mkspiffs', 'tool-mklittlefs', 'tool-mkfatfs')
$copyItems = @('platforms\espressif32') + @($requiredPackages | ForEach-Object { "packages\$_" })
foreach ($relative in $copyItems) {
    $source = Join-Path $installedCore $relative
    $destination = Join-Path $localCore $relative
    $marker = Join-Path $destination '.bll-copy-complete'
    if (-not (Test-Path -LiteralPath $marker)) {
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Required installed PlatformIO component is missing: $source"
        }
        Write-Host "Preparing local PlatformIO component: $relative"
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        Get-ChildItem -LiteralPath $source -Force | Copy-Item -Destination $destination -Recurse -Force
        New-Item -ItemType File -Path $marker -Force | Out-Null
    }
}
$taskEnv = @{
    PLATFORMIO_CORE_DIR = $localCore
    PLATFORMIO_PLATFORMS_DIR = Join-Path $localCore 'platforms'
    PLATFORMIO_PACKAGES_DIR = Join-Path $localCore 'packages'
    PLATFORMIO_SETTING_ENABLE_TELEMETRY = 'No'
    PYTHONDONTWRITEBYTECODE = '1'
}
$previousEnv = @{}
try {
    foreach ($key in $taskEnv.Keys) {
        $previousEnv[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $taskEnv[$key], 'Process')
    }
    & $pioExe @pioArguments
    $pioExitCode = $LASTEXITCODE
} finally {
    foreach ($key in $previousEnv.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previousEnv[$key], 'Process')
    }
}
exit $pioExitCode

