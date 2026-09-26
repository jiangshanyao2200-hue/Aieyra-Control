#requires -Version 7.2
[CmdletBinding()]
param(
  [string]$ElectronPath = $env:AIEYRA_CONTROL_ELECTRON,
  [string]$PythonPath = $env:AIEYRA_CONTROL_PYTHON,
  [ValidateRange(1024,65535)][int]$Port = 17910,
  [string]$Config,
  [string]$ServiceDataDir,
  [string]$UserDataDir,
  [switch]$Background,
  [switch]$Quit,
  [switch]$Wait
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$desktopRoot = Join-Path $projectRoot 'desktop'
if (-not $ElectronPath) {
  $localElectron = Join-Path $desktopRoot 'node_modules/electron/dist/electron.exe'
  if (Test-Path -LiteralPath $localElectron) { $ElectronPath = $localElectron }
}
if (-not $ElectronPath -or -not (Test-Path -LiteralPath $ElectronPath -PathType Leaf)) {
  throw '请在 desktop 运行 npm install，或用 -ElectronPath / AIEYRA_CONTROL_ELECTRON 指定现有 Electron 可执行文件。'
}
if (-not $PythonPath) { $PythonPath = (Get-Command python -ErrorAction Stop).Source }
$launchArguments = @($desktopRoot, "--python=$PythonPath", "--port=$Port")
if ($Background) { $launchArguments += '--background' }
if ($Quit) { $launchArguments += '--quit' }
if ($UserDataDir) { $launchArguments += "--user-data-dir=$([IO.Path]::GetFullPath($UserDataDir))" }
if ($Config) { $launchArguments += "--config=$([IO.Path]::GetFullPath($Config))" }
if ($ServiceDataDir) { $launchArguments += "--service-data-dir=$([IO.Path]::GetFullPath($ServiceDataDir))" }
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = [IO.Path]::GetFullPath($ElectronPath)
$startInfo.WorkingDirectory = $projectRoot
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.Environment.Remove('ELECTRON_RUN_AS_NODE') | Out-Null
foreach ($argument in $launchArguments) { $startInfo.ArgumentList.Add($argument) }
$process = [Diagnostics.Process]::Start($startInfo)
[PSCustomObject]@{pid=$process.Id;project=$projectRoot;port=$Port;background=[bool]$Background;quitRequested=[bool]$Quit}
if ($Wait) { $process.WaitForExit(); exit $process.ExitCode }
