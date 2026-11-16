# Автосинхронизация репозитория с GitHub.
# Запускается Планировщиком Windows каждые 30 минут (через sync_hidden.vbs).
#
# Порядок важен: сначала коммит локальных правок, потом pull, потом push.
# git pull --rebase НЕ работает при незакоммиченных правках ("cannot pull
# with rebase", код 128) — из-за этого синхронизация два месяца падала в
# самом начале и локальные коммиты не уезжали на сервер.
# Секреты (.env) в git не попадают.

param(
    [string]$RepoPath = "D:\Vcode\TRD bot"
)

$repo = $RepoPath
$log = Join-Path $repo "logs\sync.log"

# Без этого git в скрытом окне ждёт ввода логина/пароля и задача виснет.
$env:GIT_TERMINAL_PROMPT = "0"
$env:GCM_INTERACTIVE = "never"

function Log($message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $log -Value "$stamp | $message" -Encoding utf8
}

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    # git.exe явно: функция с именем Git перекрыла бы саму команду
    $lines = & git.exe @GitArgs 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            $_.Exception.Message
        }
        else {
            [string]$_
        }
    }
    return [pscustomobject]@{
        Code   = $LASTEXITCODE
        Output = (@($lines) -join "`n").Trim()
    }
}

Set-Location $repo

# --- 0. Реанимация: прошлый неудачный pull мог оставить незавершённый rebase ---
if ((Test-Path (Join-Path $repo ".git\rebase-merge")) -or
    (Test-Path (Join-Path $repo ".git\rebase-apply"))) {
    $abort = Invoke-Git rebase --abort
    if ($abort.Code -ne 0) {
        Log "ОШИБКА: git rebase --abort (код $($abort.Code)): $($abort.Output)"
        exit 1
    }
    Log "rebase был не завершён — прерван"
}

# --- 1. Локальные правки -> коммит (иначе pull --rebase откажется) ---
$status = Invoke-Git status --porcelain
if ($status.Code -ne 0) {
    Log "ОШИБКА: git status (код $($status.Code)): $($status.Output)"
    exit 1
}
if ($status.Output) {
    $changed = @($status.Output -split "`r?`n" | Where-Object { $_ }).Count
    $add = Invoke-Git add -A
    if ($add.Code -ne 0) {
        Log "ОШИБКА: git add (код $($add.Code)): $($add.Output)"
        exit 1
    }
    $commit = Invoke-Git commit -m "auto-sync $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    if ($commit.Code -ne 0) {
        Log "ОШИБКА: git commit (код $($commit.Code)): $($commit.Output)"
        exit 1
    }
    Log "commit: локальных правок $changed"
}

# --- 2. Забрать изменения с сервера ---
# --autostash: за время запуска мог измениться любой файл (например, сам
# sync.log), и rebase не должен из-за этого падать.
$pull = Invoke-Git pull --rebase --autostash --quiet origin main
if ($pull.Code -ne 0) {
    Log "ОШИБКА: git pull (код $($pull.Code)): $($pull.Output)"
    $abort = Invoke-Git rebase --abort
    if ($abort.Code -eq 0) {
        Log "незавершённый rebase прерван"
    }
    exit 1
}
Log "pull: ok"

# --- 3. Отправить на сервер (пусто -> git сам скажет "Everything up-to-date") ---
$push = Invoke-Git push --quiet origin main
if ($push.Code -ne 0) {
    Log "ОШИБКА: git push (код $($push.Code)): $($push.Output)"
    exit 1
}
Log "push: ok"
