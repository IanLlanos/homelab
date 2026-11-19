# utilities

| Script | What it does |
|---|---|
| `Restart-Plex-If-Crashed.ps1` | Starts Plex Media Server if it isn't running. Run it from a scheduled task every few minutes. |
| `start-claude-minimized.ps1` | Launches the Claude desktop app and minimizes its window. Run it from an at-logon task. |
| `dedupe.ps1` | Removes duplicate media copies listed in `dedupe-plan.csv`: keeps the larger (higher-quality) file; if sizes are within 5%, keeps the copy on the drive with more free space. Moves the better copy into place when Sonarr/Radarr tracks the one being removed, then triggers rescans. |

## Deploy
Copy the scripts to `C:\Scripts` (dedupe into `C:\Scripts\dedupe`). No environment variables or secrets; dedupe reads the Sonarr/Radarr API keys from their `config.xml` at run time.

```powershell
# Plex watchdog, every 5 minutes
$a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Scripts\Restart-Plex-If-Crashed.ps1'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask 'Restart Plex If Crashed' -Action $a -Trigger $t -Principal (New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive)

# Claude minimized at logon
$a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\Scripts\start-claude-minimized.ps1'
$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; $t.Delay = 'PT1M'
Register-ScheduledTask 'Start Claude' -Action $a -Trigger $t
```

dedupe: build `dedupe-plan.csv` (format in `dedupe-plan.example.csv`), preview, then run:
```powershell
powershell -ExecutionPolicy Bypass -File C:\Scripts\dedupe\dedupe.ps1
powershell -ExecutionPolicy Bypass -File C:\Scripts\dedupe\dedupe.ps1 -Run
```
