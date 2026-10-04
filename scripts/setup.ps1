param([switch]$NoOpenConfig)

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
        # Python 3.12 is preferred for optional OCR wheels; newer Python is supported.
        foreach ($version in @('-3.12', '-3')) {
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
    if (-not $pythonPath) { throw 'Install Python 3.12 or newer with the Python launcher, then run setup.bat again.' }
    Invoke-Checked $pythonPath @('-c', 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ is required"')
    Invoke-Checked $pythonPath @('-m', 'venv', (Join-Path $projectRoot '.venv'))
}

Invoke-Checked $venvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
Invoke-Checked $venvPython @('-m', 'pip', 'install', '-r', (Join-Path $projectRoot 'requirements.txt'))
Invoke-Checked $venvPython @('-m', 'playwright', 'install', 'chromium')

$envFile = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $envFile
    Write-Host 'Fill TELEGRAM_API_ID and TELEGRAM_API_HASH in .env before connecting accounts.'
    if (-not $NoOpenConfig) { Start-Process notepad.exe -ArgumentList ('"{0}"' -f $envFile) }
}
Invoke-Checked $venvPython @((Join-Path $PSScriptRoot 'prepare_layout.py'))
& (Join-Path $PSScriptRoot 'create_shortcut.ps1')
