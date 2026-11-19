# weekly-update

Runs Windows Update once a week. Before restarting it waits for Plex streams to finish (checked through Tautulli), stops Docker containers and the Sonarr/Radarr services cleanly, then restarts. After the restart it brings everything back, runs health checks, and posts a "finished" message to Discord with an Errors field. An optional second webhook posts short, non-technical down/up messages to a public announcements channel.

## Deploy
1. Copy this folder to `C:\Scripts\WeeklyUpdate`.
2. `Copy-Item config.example.json config.json` and fill in the values below.
3. Test (changes nothing):
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Scripts\WeeklyUpdate\Invoke-WeeklyUpdate.ps1 -Test
   powershell -ExecutionPolicy Bypass -File C:\Scripts\WeeklyUpdate\Invoke-WeeklyUpdate.ps1 -DryRun
   ```
4. Register the two scheduled tasks (elevated PowerShell):
   ```powershell
   $ps = 'powershell.exe'; $f = 'C:\Scripts\WeeklyUpdate\Invoke-WeeklyUpdate.ps1'
   $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
   $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 4) -StartWhenAvailable
   Register-ScheduledTask 'Weekly Update' -Principal $p -Settings $s `
     -Action (New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$f`" -Mode Install") `
     -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 3am)
   $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; $t.Delay = 'PT2M'
   Register-ScheduledTask 'Weekly Update - After Restart' -Principal $p -Settings $s -Trigger $t `
     -Action (New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$f`" -Mode After")
   ```
5. Set Windows Update to download but not auto-install, so only this script restarts the server.

## Settings (config.json)
No environment variables. Secrets go in `config.json` (git-ignored):

| Key | Value |
|---|---|
| `WebhookUrl` | Discord webhook for detailed status/alerts |
| `PublicWebhookUrl` | Optional webhook for short public announcements |
| `TautulliConfigIni` | Path to Tautulli's `config.ini` (its API key is read at run time) |
| `Services`, `HealthChecks` | Windows services to stop and URLs/TCP ports to check after the restart |
