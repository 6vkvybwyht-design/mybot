# Автозапуск бота при вході у Windows (задача Планувальника завдань «USOUSD bot»).
# Бот працює у вашій сесії, поруч із терміналом MT5, і з тими самими правами (не від адміністратора).
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script = Join-Path $root "deploy\windows\run-bot.ps1"
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$script`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = "PT1M"                             # дати терміналу MT5 запуститися і залогінитися
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "USOUSD bot" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Готово: задача «USOUSD bot» запускатиме бота через 1 хв після входу в Windows."
Write-Host "Запустити зараз: Start-ScheduledTask -TaskName 'USOUSD bot'"
