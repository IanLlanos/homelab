# drive-health

Weekly SMART check of every physical disk using `smartctl` (read-only). Maps each disk to its drive letters, remembers the last result per serial number, and posts to Discord only when something gets worse: new SMART errors, reallocated/pending sectors, high temperature, low free space, NVMe wear, or a drive going missing.

## Deploy
1. Install smartmontools: `winget install smartmontools.smartmontools`
2. Copy this folder to `C:\Scripts\DriveHealth`.
3. `Copy-Item config.example.json config.json` and set `DiscordWebhookUrl`.
4. Test, then schedule it (must run elevated so smartctl can open the disks):
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Scripts\DriveHealth\Check-DriveHealth.ps1 -DryRun
   powershell -ExecutionPolicy Bypass -File C:\Scripts\DriveHealth\Check-DriveHealth.ps1 -Test
   $a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Scripts\DriveHealth\Check-DriveHealth.ps1'
   $p = New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest
   Register-ScheduledTask 'DriveHealth Check' -Action $a -Principal $p -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3am)
   ```
   `-Report` posts a summary of all drives.

## Settings (config.json)
No environment variables. Secrets go in `config.json` (git-ignored):

| Key | Value |
|---|---|
| `DiscordWebhookUrl` | Discord webhook for alerts |
| `ExcludeSerials` | Serial numbers of drives to ignore |
| `PriorityLetters` | Drives listed first in alerts |
| `Thresholds` | `HddTempC`, `LowFreePct`, `NvmeWearPct` |
