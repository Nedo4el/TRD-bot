' Запуск sync_github.ps1 без окна консоли (в т.ч. при Windows Terminal по умолчанию).
' Вызывается Планировщиком Windows.
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " _
    & Chr(34) & "D:\Vcode\TRD bot\sync_github.ps1" & Chr(34)
sh.Run cmd, 0, False
