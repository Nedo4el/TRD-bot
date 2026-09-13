# auto-sync.ps1 — автосинхронизация с git каждые 2 часа
$repo = "D:\Vcode\TRD bot"
Set-Location $repo

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    git add -A
    $changes = git status --porcelain
    if ($changes) {
        git commit -m "auto-sync $timestamp"
        git push
        Write-Host "[$timestamp] Synced to git"
    } else {
        Write-Host "[$timestamp] No changes"
    }
    Start-Sleep -Seconds 7200
}
