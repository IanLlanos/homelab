$plexProcess = Get-Process "Plex Media Server" -ErrorAction SilentlyContinue

if (-not $plexProcess) {
    Start-Process -FilePath "C:\Program Files\Plex\Plex Media Server\Plex Media Server.exe"
}