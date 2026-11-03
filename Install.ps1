#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$PythonExecutable = 'python',
    [ValidateSet('logon','none')][string]$StartupMode = 'logon',
    [switch]$PrepareOnly,
    [string]$Proxy
)
$ErrorActionPreference = 'Stop'
$environmentPath = Join-Path $PSScriptRoot 'runtime\venv'
$python = Join-Path $environmentPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    & $PythonExecutable -m venv --without-pip $environmentPath
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $python (Join-Path $PSScriptRoot 'runner.py') --root $PSScriptRoot doctor --offline
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($PrepareOnly) { Write-Output 'Local environment prepared. Next: .\run.ps1 setup'; exit 0 }
$setupArguments = @((Join-Path $PSScriptRoot 'runner.py'), '--root', $PSScriptRoot, 'setup')
if ($Proxy) { $setupArguments += @('--proxy', $Proxy) }
& $python @setupArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($StartupMode -ne 'none') {
    & $python (Join-Path $PSScriptRoot 'runner.py') --root $PSScriptRoot service install --mode $StartupMode
    exit $LASTEXITCODE
}
Write-Output 'Binding completed. Next: .\run.ps1 run'
