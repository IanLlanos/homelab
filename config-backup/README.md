# config-backup

Weekly backup of the server's app configs into one zip (keeps the newest `KeepCount`): Sonarr/Radarr/Prowlarr (via their own backup API), Tautulli, Plex preferences + automatic DB backup, Docker volume contents, compose folders, and the Homebridge folder from its VM over SSH. Posts to Discord when something goes wrong (optionally on start/success too). Restore steps are in [RESTORE.md](RESTORE.md).

## Deploy
1. Copy this folder to `C:\Scripts\backuper`.
2. `Copy-Item config.example.json config.json` and fill in the values below.
3. For the Homebridge step, create an SSH key and add it to the VM:
   ```powershell
   ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\id_ed25519_homebridge -N '""'
   type $env:USERPROFILE\.ssh\id_ed25519_homebridge.pub | ssh <user>@<HOMEBRIDGE_HOST_IP> "cat >> ~/.ssh/authorized_keys"
   ```
4. Test the webhook, then schedule it weekly (elevated):
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Scripts\backuper\Backup-ServerConfig.ps1 -Test
   $a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Scripts\backuper\Backup-ServerConfig.ps1'
   $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest
   Register-ScheduledTask 'Config Backup' -Action $a -Principal $p -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 2am)
   ```

## Settings (config.json)
No environment variables. Secrets go in `config.json` (git-ignored):

| Key | Value |
|---|---|
| `WebhookUrl` | Discord webhook for alerts |
| `BackupFolder`, `KeepCount`, `MinFreeGB` | Where zips go and how many to keep |
| `Homebridge.Host` / `User` / `KeyFile` / `ParentDir` | SSH target for the Homebridge folder |

App API keys are read from each app's own `config.xml`/`config.ini` at run time.

> The backup zips themselves contain secrets. Keep them off GitHub.
