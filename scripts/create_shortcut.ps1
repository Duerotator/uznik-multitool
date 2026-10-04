$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonWindowless = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'Run setup.bat first.' }
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$shortcutPaths = @((Join-Path $desktop 'Uznik MultiTool.lnk'), (Join-Path $projectRoot 'Uznik MultiTool.lnk'))
foreach ($shortcutPath in $shortcutPaths) {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $pythonWindowless
    $shortcut.Arguments = '"{0}"' -f (Join-Path $projectRoot 'app\launch.pyw')
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.Description = 'Uznik MultiTool - desktop Telegram toolkit'
    $iconPath = Join-Path $projectRoot 'assets\branding\uznik-multitool.ico'
    if (Test-Path -LiteralPath $iconPath) { $shortcut.IconLocation = "$iconPath,0" }
    else { $shortcut.IconLocation = "$pythonWindowless,0" }
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    Write-Host "Shortcut: $shortcutPath"
}
