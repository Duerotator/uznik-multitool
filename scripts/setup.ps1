param([switch]$NoOpenConfig, [switch]$SkipNativeTools)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $Executable (exit $LASTEXITCODE)" }
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonPath = $null
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($version in @('-3.14', '-3.13', '-3.12')) {
            try { $candidate = & py $version -c 'import sys; print(sys.executable)' 2>$null }
            catch { $candidate = $null; continue }
            if ($LASTEXITCODE -eq 0 -and $candidate) {
                $pythonPath = [string]($candidate | Select-Object -Last 1)
                break
            }
        }
    }
    if (-not $pythonPath -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $candidate = & python -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $candidate) { $pythonPath = [string]($candidate | Select-Object -Last 1) }
    }
    if (-not $pythonPath) { throw 'Install CPython 3.12-3.14 x64 with the Python launcher, then run setup.bat again.' }
    Invoke-Checked $pythonPath @('-c', 'import sys; assert (3,12) <= sys.version_info[:2] <= (3,14) and sys.maxsize > 2**32 and sys.implementation.name == "cpython", "CPython 3.12-3.14 x64 is required"')
    Invoke-Checked $pythonPath @('-m', 'venv', (Join-Path $projectRoot '.venv'))
}

Invoke-Checked $venvPython @('-X', 'utf8', (Join-Path $PSScriptRoot 'check_install.py'), '--namespace-only')
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
Invoke-Checked $venvPython @('-m', 'pip', 'install', '-r', (Join-Path $projectRoot 'config\requirements.txt'))
Invoke-Checked $venvPython @('-m', 'pip', 'check')
Invoke-Checked $venvPython @('-m', 'playwright', 'install', 'chromium')
if (-not $SkipNativeTools) {
    & (Join-Path $PSScriptRoot 'windows\install_native_tools.ps1')
}

$envFile = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'config\.env.example') -Destination $envFile
    Write-Host 'Fill TELEGRAM_API_ID and TELEGRAM_API_HASH in .env before connecting accounts.'
    if (-not $NoOpenConfig) { Start-Process notepad.exe -ArgumentList ('"{0}"' -f $envFile) }
}
Invoke-Checked $venvPython @((Join-Path $PSScriptRoot 'prepare_layout.py'))
$checkArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'check_install.py'))
if (-not $SkipNativeTools) { $checkArguments += '--require-native' }
Invoke-Checked $venvPython $checkArguments
& (Join-Path $PSScriptRoot 'create_shortcut.ps1')
if ($SkipNativeTools) { Write-Warning 'Native tools were skipped. Install FFmpeg, Tesseract and Xray before using their features.' }
