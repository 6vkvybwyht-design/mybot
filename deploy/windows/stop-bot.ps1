# Зупиняє бота: задачу Планувальника і процес python -m bot.main (щоб не було двох копій).
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\stop-bot.ps1
if (Get-ScheduledTask -TaskName "USOUSD bot" -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName "USOUSD bot"
}
# Спершу — цикл перезапуску run-bot.ps1, інакше він підніме бота знову
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' OR Name = 'pwsh.exe'" |
    Where-Object { $_.CommandLine -like "*run-bot.ps1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*-m bot.main*" }
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force }
Write-Host "Зупинено процесів бота: $(@($procs).Count)"
