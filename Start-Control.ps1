#requires -Version 7.2
[CmdletBinding()]
param([switch]$Background, [switch]$Quit, [string]$ElectronPath, [string]$PythonPath)
$ErrorActionPreference = 'Stop'
$arguments = @{ Background = $Background; Quit = $Quit }
if ($PythonPath) { $arguments.PythonPath = $PythonPath }
if ($ElectronPath) { $arguments.ElectronPath = $ElectronPath }
elseif ($env:AIEYRA_CONTROL_ELECTRON) { $arguments.ElectronPath = $env:AIEYRA_CONTROL_ELECTRON }
elseif (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'desktop/node_modules/electron/dist/electron.exe'))) {
    $privateData = $env:AIEYRA_CONTROL_DATA
    if (-not $privateData) {
        $locator = if ($env:AIEYRA_CONTROL_STORAGE) { $env:AIEYRA_CONTROL_STORAGE } else { Join-Path $env:APPDATA 'Aieyra Control/storage.json' }
        if (Test-Path -LiteralPath $locator) {
            $privateData = (Get-Content -LiteralPath $locator -Raw | ConvertFrom-Json).data_root
        } else { $privateData = Join-Path $env:LOCALAPPDATA 'Aieyra Control/data' }
    }
    $deployment = Join-Path $privateData 'config/desktop.local.json'
    if (Test-Path -LiteralPath $deployment) {
        $settings = Get-Content -LiteralPath $deployment -Raw | ConvertFrom-Json
        $arguments.ElectronPath = $settings.electron_executable
    }
}
& (Join-Path $PSScriptRoot 'scripts/start-desktop.ps1') @arguments
