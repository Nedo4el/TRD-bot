# Запуск robot_zakol в фоне. Ярлык ведёт на этот скрипт.
# -Quiet: без окна MessageBox (для запуска из консоли).
param([switch]$Quiet)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Get-ZakolProcess {
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -eq 'python.exe' -or $_.Name -eq 'uv.exe') -and
            $_.CommandLine -like '*robot_zakol.main*'
        }
}

function Show-Status([string]$msg) {
    if ($Quiet) {
        Write-Host $msg
    } else {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($msg, 'Robot Zakol') | Out-Null
    }
}

$running = @(Get-ZakolProcess)
if ($running.Count -gt 0) {
    Show-Status "Бот уже запущен (PID $($running.ProcessId -join ', '))."
    exit 0
}

# снимаем флаг стопа прошлой сессии — иначе бот сразу встанет в STOPPED
$kill = Join-Path $root 'data\zakol.kill'
if (Test-Path $kill) { Remove-Item $kill -Force }

$py = Join-Path $root '.venv\Scripts\python.exe'
$stdout = Join-Path $root 'logs\zakol_stdout.log'
$stderr = Join-Path $root 'logs\zakol_stderr.log'
$p = Start-Process -FilePath $py `
    -ArgumentList @('-m', 'robot_zakol.main') `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

Start-Sleep -Seconds 6
if (-not (Get-Process -Id $p.Id -ErrorAction SilentlyContinue)) {
    $err = Get-Content $stderr -Tail 15 -ErrorAction SilentlyContinue
    Show-Status "Бот не стартовал:`n`n$($err -join "`n")"
    exit 1
}

Show-Status "Запущен, PID $($p.Id).`nЛог: logs\zakol.log"
