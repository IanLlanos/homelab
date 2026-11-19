# announcements

Posts a one-time @everyone announcement with an embed and banner image to a Discord channel, here for the yearly Plex Wrapped (Wrapperr) page.

## Deploy
1. Copy this folder to `C:\Scripts\Announcements` and add a banner image named `wrapped-2026.png` (not included).
2. Replace `<YOUR_DOMAIN>` and `<SERVER_NAME>` in the script.
3. Test (posts to the private alert webhook, no @everyone), then schedule the real post:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Scripts\Announcements\Send-WrappedAnnouncement.ps1 -Test
   $a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Scripts\Announcements\Send-WrappedAnnouncement.ps1'
   $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U
   Register-ScheduledTask 'Wrapped Announcement' -Action $a -Principal $p -Trigger (New-ScheduledTaskTrigger -Once -At '2026-12-04 12:00')
   ```

## Settings
No environment variables. Webhooks are read at run time from the [weekly-update](../weekly-update/) `config.json`: `PublicWebhookUrl` for the real post, `WebhookUrl` for `-Test`.
