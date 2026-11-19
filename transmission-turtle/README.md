# transmission-turtle

Keeps Plex streams smooth: turns Transmission's turtle mode (alternate speed limits) on while anyone is watching Plex and off when nobody is. Each run checks Tautulli for active streams and sets Transmission to match, so it's safe to run as often as you like.

## Deploy
1. Copy this folder to `C:\Scripts\TransmissionTurtle`.
2. `Copy-Item config.example.json config.json` and adjust paths/speeds.
3. Check it:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Scripts\TransmissionTurtle\Set-TransmissionTurtle.ps1 -Status
   ```
4. Run it every minute, hidden (S4U, no stored password):
   ```powershell
   $a = New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Scripts\TransmissionTurtle\Set-TransmissionTurtle.ps1'
   $t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
   $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U
   Register-ScheduledTask 'Transmission Turtle' -Action $a -Trigger $t -Principal $p
   ```

## Settings (config.json)
No environment variables and no secrets in the file. At run time the script reads:
- the Tautulli API key from `TautulliConfigIni`
- the Transmission RPC username/password (`USER`/`PASS`) from the Transmission `docker-compose.yml` at `TransmissionCompose`

`TurtleDownKBps` / `TurtleUpKBps` set the turtle-mode limits.
