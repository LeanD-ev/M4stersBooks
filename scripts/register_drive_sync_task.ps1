param(
    [switch]$Unregister
)

$taskName = "M4 Books Drive Sync"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Scheduled task removed: $taskName"
    exit 0
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $repoRoot "drive_sync.py"
$pythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Drive sync script not found: $scriptPath"
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Python was not found. Install Python or create the project's .venv first."
    }
    $pythonPath = $pythonCommand.Source
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-u "' + $scriptPath + '"'
$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument $arguments `
    -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal `
    -UserId $user `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Monitora livros e metadados do M4 Books e sincroniza com o Drive." `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Output "Scheduled task registered and started: $taskName"
