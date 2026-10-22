# Запускає бота і перезапускає його після збою.
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\windows\run-bot.ps1
# Зупинка: Ctrl+C у цьому вікні (або deploy\windows\stop-bot.ps1).
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root
$env:PYTHONUTF8 = "1"
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Не знайдено $python — створіть віртуальне середовище (див. README)."
    exit 1
}
while ($true) {
    & $python -m bot.main
    $code = $LASTEXITCODE
    if ($code -eq 0) { break }                      # звичайна зупинка (Ctrl+C)
    if ($code -eq 2) {
        Write-Host "Помилка конфігурації — виправте .env або config.yaml і запустіть знову."
        exit 2
    }
    Write-Host "$(Get-Date -Format s) бот завершився з кодом $code; перезапуск через 15 с"
    Start-Sleep -Seconds 15
}
