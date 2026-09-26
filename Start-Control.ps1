#requires -Version 7.2
[CmdletBinding()]
param([switch]$Background, [switch]$Quit, [string]$ElectronPath, [string]$PythonPath)
$ErrorActionPreference = 'Stop'
$arguments = @{ Background = $Background; Quit = $Quit }
if ($PythonPath) { $arguments.PythonPath = $PythonPath }
if ($ElectronPath) { $arguments.ElectronPath = $ElectronPath }
elseif ($env:AIEYRA_CONTROL_ELECTRON) { $arguments.ElectronPath = $env:AIEYRA_CONTROL_ELECTRON }
elseif (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'desktop/node_modules/electron/dist/electron.exe'))) {
    $deployment = Join-Path $PSScriptRoot 'config/desktop.local.json'
    if (Test-Path -LiteralPath $deployment) {
        $settings = Get-Content -LiteralPath $deployment -Raw | ConvertFrom-Json
        $arguments.ElectronPath = $settings.electron_executable
    }
}
& (Join-Path $PSScriptRoot 'scripts/start-desktop.ps1') @arguments
