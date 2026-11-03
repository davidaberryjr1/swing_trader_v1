param(
    [string]$TaskName = "SwingTraderPaperBot"
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Repository ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing repository virtual environment: $Python"
}
if (-not (Test-Path -LiteralPath (Join-Path $Repository "config.py"))) {
    throw "Configure Alpaca paper credentials in the ignored config.py first."
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task '$TaskName' already exists. Review or remove it before registering a replacement."
}

$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $Python -Argument '-u main.py --paper-bot' -WorkingDirectory $Repository
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings | Out-Null
Write-Output "Registered '$TaskName' for future logons. It has NOT been started."
Write-Output "Start-ScheduledTask -TaskName '$TaskName' starts paper order placement."
Write-Output "Disable-ScheduledTask -TaskName '$TaskName' disables future starts."
Write-Output "Stop-ScheduledTask -TaskName '$TaskName' stops the process, not broker-held orders."
