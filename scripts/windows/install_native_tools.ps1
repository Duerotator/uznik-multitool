# Install external executables through the Windows Package Manager.
# Existing tools are left alone; no gateway, services or accounts are started.
$ErrorActionPreference = 'Stop'

function Refresh-ToolPath {
    $env:Path = (@(
        $env:Path,
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links')
    ) | Where-Object { $_ }) -join ';'
}

function Find-NativeTool {
    param([string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    if ($Name -eq 'tesseract') {
        foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
            if (-not $base) { continue }
            foreach ($relative in @('Tesseract-OCR\tesseract.exe', 'Programs\Tesseract-OCR\tesseract.exe')) {
                $candidate = Join-Path $base $relative
                if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
            }
        }
    }
    return $null
}

Refresh-ToolPath
$tools = @(
    @{ Name = 'ffmpeg'; Id = 'Gyan.FFmpeg' },
    @{ Name = 'tesseract'; Id = 'UB-Mannheim.TesseractOCR' },
    @{ Name = 'xray'; Id = 'XTLS.Xray-core' }
)
foreach ($tool in $tools) {
    if (Find-NativeTool $tool.Name) {
        Write-Host "$($tool.Name): already installed"
        continue
    }
    $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'WinGet is required. Install Microsoft App Installer, or install the native tools manually (docs/INSTALLATION.md).'
    }
    Write-Host "Installing $($tool.Id)..."
    & $winget.Source install --id $tool.Id --exact --source winget --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) { throw "WinGet could not install $($tool.Id) (exit $LASTEXITCODE). See docs/INSTALLATION.md." }
    Refresh-ToolPath
    if (-not (Find-NativeTool $tool.Name)) {
        throw "$($tool.Name) was not found after installation. Reopen the terminal and run setup.bat again, or check PATH."
    }
}
