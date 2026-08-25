# Автосинхронизация репозитория с GitHub.
# Запускается Планировщиком Windows каждые 30 минут.
# Тянет изменения с сервера, при наличии локальных изменений -
# коммитит их и отправляет. Секреты (.env) в git не попадают.

$repo = "D:\Vcode\TRD bot"
$log = Join-Path $repo "logs\sync.log"

function Log($message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "$stamp | $message" -Encoding utf8
}

Set-Location $repo

# --- 1. Забрать изменения с сервера ---
git pull --rebase --quiet origin main
if ($LASTEXITCODE -ne 0) {
    Log "ОШИБКА: git pull завершился с кодом $LASTEXITCODE (конфликт или сеть)"
    exit 1
}
Log "pull: ok"

# --- 2. Локальные изменения -> коммит и отправка ---
$status = git status --porcelain
if ($status) {
    git add -A
    if ($LASTEXITCODE -ne 0) {
        Log "ОШИБКА: git add завершился с кодом $LASTEXITCODE"
        exit 1
    }
    git commit -m "auto-sync $(Get-Date -Format 'yyyy-MM-dd HH:mm')" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "ОШИБКА: git commit завершился с кодом $LASTEXITCODE"
        exit 1
    }
    git push --quiet origin main
    if ($LASTEXITCODE -ne 0) {
        Log "ОШИБКА: git push завершился с кодом $LASTEXITCODE"
        exit 1
    }
    Log "push: отправлено файлов: $(@($status).Count)"
}
else {
    Log "локальных изменений нет"
}
