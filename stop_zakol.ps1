# Остановка robot_zakol: kill-файл → бот снимает лимитки, закрывает позицию
# по рынку и убирает заявки (при неудаче закрытия — SL/TP остаются).
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Get-ZakolProcess {
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -eq 'python.exe' -or $_.Name -eq 'uv.exe') -and
            $_.CommandLine -like '*robot_zakol.main*'
        }
}

$procs = @(Get-ZakolProcess)
if ($procs.Count -eq 0) {
    Write-Host 'Бот не запущен.'
    exit 0
}

$kill = Join-Path $root 'data\zakol.kill'
New-Item -ItemType File -Path $kill -Force | Out-Null
Write-Host "kill-файл: $kill — жду корректной остановки (до 40с)..."

$deadline = (Get-Date).AddSeconds(40)
do {
    Start-Sleep -Milliseconds 500
    $procs = @(Get-ZakolProcess)
} while ($procs.Count -gt 0 -and (Get-Date) -lt $deadline)

if ($procs.Count -gt 0) {
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host 'Не остановился за 40с — убил принудительно. Проверь ордера на бирже!'
} else {
    Write-Host 'Остановлен чисто: лимитки сняты, позиция закрыта, заявки убраны.'
}
