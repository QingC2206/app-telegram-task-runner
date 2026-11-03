#requires -Version 5.1
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot 'runtime\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run .\Install.ps1 -PrepareOnly first.' }
& $python (Join-Path $PSScriptRoot 'runner.py') --root $PSScriptRoot @args
exit $LASTEXITCODE
