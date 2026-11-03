#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('install','start','stop','status','uninstall')][string]$Action,
    [Parameter(Mandatory=$true)][string]$SpecPath
)
$ErrorActionPreference = 'Stop'
$spec = Get-Content -LiteralPath $SpecPath -Raw -Encoding UTF8 | ConvertFrom-Json
$taskName = [string]$spec.name
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    $actions = @($task.Actions)
    $sameInterpreter = $actions.Count -eq 1 -and [IO.Path]::GetFullPath($actions[0].Execute) -eq [IO.Path]::GetFullPath([string]$spec.argv[0])
    # Compare the full expected arguments, not a substring of a path or module name.
    $expectedArguments = [string]$spec.arguments
    if (-not $sameInterpreter -or [string]$actions[0].Arguments -cne [string]$expectedArguments) {
        throw 'Existing scheduled task is not owned by this application instance.'
    }
}
if ($Action -eq 'status') {
    if (-not $task) { Write-Output 'Not installed'; exit 1 }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    [pscustomobject]@{Name=$taskName; State=[string]$task.State; LogonType=[string]$task.Principal.LogonType; LastTaskResult=$info.LastTaskResult; LastRunTime=$info.LastRunTime} | Format-List
    exit 0
}
if ($Action -eq 'uninstall') {
    if ($task) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Write-Output 'Managed task removed; configuration, plugins and logs retained.'
    exit 0
}
if ($Action -eq 'stop') {
    if ($task) { Disable-ScheduledTask -TaskName $taskName | Out-Null; Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
    exit 0
}
if ($Action -eq 'start') {
    if (-not $task) { throw 'Install the task first.' }
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    Start-ScheduledTask -TaskName $taskName
    exit 0
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$account = $identity.Name
$python = ([string]$spec.argv[0]).Replace('pythonw.exe','python.exe')
$xmlPath = Join-Path (Split-Path -Parent $SpecPath) 'scheduled-task.xml'
& $python (Join-Path ([string]$spec.root) 'runner.py') --root ([string]$spec.root) service export --mode logon --account $account --output $xmlPath
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$xml = Get-Content -LiteralPath $xmlPath -Raw -Encoding UTF8
Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output ('Installed and started: ' + $taskName)
