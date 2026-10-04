$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonWindowless = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'Run setup.bat first.' }
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Uznik MultiTool.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonWindowless
$shortcut.Arguments = '"{0}"' -f (Join-Path $projectRoot 'launch.pyw')
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = 'Uznik MultiTool - desktop Telegram toolkit'
$shortcut.IconLocation = "$pythonWindowless,0"
$shortcut.WindowStyle = 1
$shortcut.Save()
Write-Host "Shortcut: $shortcutPath"
