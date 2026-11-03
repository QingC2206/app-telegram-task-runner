#requires -Version 5.1
[CmdletBinding()]
param([string]$ExpectedUserSid)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ($ExpectedUserSid -and $identity.User.Value -ne $ExpectedUserSid) {
    throw 'Continue under the same Windows account that owns the repositories and bot credentials.'
}
$runnerRoot = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $runnerRoot 'run.ps1'
Write-Host ('Windows account: ' + $identity.Name)
Write-Host 'Step 1: Bind the bot if not configured. Token is entered only in this terminal.'
if (-not (Test-Path -LiteralPath (Join-Path $runnerRoot 'runtime\robot.json') -PathType Leaf)) {
    & $entry setup
    if ($LASTEXITCODE -ne 0) { throw 'Binding is not complete. Fix the displayed issue before installing automatic login startup.' }
}
Write-Host 'Step 2: Verify the saved bot connection.'
& $entry doctor --online
if ($LASTEXITCODE -ne 0) { throw 'Connection check failed. Login startup was not registered.' }
Write-Host 'Step 3: Register automatic startup after Windows login. No account password is collected.'
& $entry service install
if ($LASTEXITCODE -ne 0) { throw 'Login startup registration did not complete. Binding was preserved.' }
Write-Host 'Step 4: Check the registered task.'
& $entry service status
if ($LASTEXITCODE -ne 0) { throw 'Could not verify the registered task.' }
Write-Host 'Login startup installation finished.'
