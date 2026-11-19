# Restoring from a ServerConfig backup

Each backup is one zip: `X:\Backups\ServerConfig\ServerConfig-YYYY-MM-DD_HHmm.zip`.
Extract it somewhere temporary (e.g. `C:\Restore`). `MANIFEST.txt` inside shows which steps succeeded that week.

> The zip holds secrets (API keys, Plex token, .env files, Tesla tokens). Delete the extracted copy when done.

| Folder in zip | Contents |
|---|---|
| `Sonarr\`, `Radarr\`, `Prowlarr\` | The app's own backup zip |
| `Tautulli\` | `tautulli.backup-*.db` + `config.backup-*.ini` |
| `Plex\` | `PlexMediaServer-Preferences.reg` + `com.plexapp.plugins.library[.blobs].db-DATE` |
| `DockerVolumes\overseerr`, `DockerVolumes\wizarr` | Contents of the Seerr / Wizarr data volumes |
| `Files\...` | Compose files, .env files and bind-mounted config (C:\Docker, Desktop\Prowlarr, Transmission, Calendarr, Requestrr) |
| `Homebridge\homebridge.tar.gz` | `/homebridge` from the VM (without node_modules / .venv) |

---

## Sonarr / Radarr / Prowlarr
1. Install the app (or start the container for Prowlarr) and open its web UI.
2. **System > Backup > Restore Backup**, choose the zip from the matching folder.
3. The app restarts with the restored database and settings.
   (Sonarr/Radarr cover art in MediaCover re-downloads by itself.)

## Tautulli
1. Quit Tautulli (tray icon > Quit).
2. In `%LOCALAPPDATA%\Tautulli`, rename the current `tautulli.db` and `config.ini` (keep them just in case).
3. Copy in the backup files and rename them to `tautulli.db` and `config.ini`.
4. Start Tautulli.

## Plex
1. Quit Plex Media Server (tray icon > Exit).
2. Double-click `PlexMediaServer-Preferences.reg` and accept (restores settings and server identity/token).
3. In `%LOCALAPPDATA%\Plex Media Server\Plug-in Support\Databases`: move the current
   `com.plexapp.plugins.library.db`, `...library.blobs.db` and any `-wal` / `-shm` files aside.
4. Copy in the two backup files and rename them, dropping the `-DATE` suffix:
   `com.plexapp.plugins.library.db` and `com.plexapp.plugins.library.blobs.db`.
5. Start Plex. Posters and other metadata rebuild over time (they were not backed up).

## Seerr (overseerr) and Wizarr
```powershell
docker stop overseerr
docker cp C:\Restore\DockerVolumes\overseerr\. overseerr:/app/config
docker start overseerr
```
Same for Wizarr: `docker stop wizarr`, `docker cp C:\Restore\DockerVolumes\wizarr\. wizarr:/data`, `docker start wizarr`.
If the container must be recreated first, run `docker compose up -d` in `C:\Docker\guest-portal`, then do the above.
If the app logs permission errors afterwards, the copied files may need the container user's ownership.

## Docker compose projects (C:\Docker, Desktop\Prowlarr, Transmission, Calendarr, Requestrr)
1. Copy each folder from `Files\` back to where it came from:
   - `Files\C-Docker\` -> `C:\Docker\`
   - `Files\Prowlarr-compose\` -> `C:\Users\<user>\Desktop\Prowlarr\`
   - `Files\Transmission-compose\` -> `C:\Users\<user>\Desktop\Transmission docker\`
   - `Files\Calendarr\` -> `C:\Users\<user>\Desktop\Calendarr\`
   - `Files\Requestrr\` -> `C:\Users\<user>\Docker\requestrr\`
2. In each project folder: `docker compose up -d`.
3. Requestrr has no compose file. Recreate it with its original `docker run` command (image `thomst08/requestrr`,
   mount `C:\Users\<user>\Docker\requestrr\config` to `/root/config`), then restore its `config` folder.

## Homebridge (VM <HOMEBRIDGE_HOST_IP>)
```powershell
scp -i $env:USERPROFILE\.ssh\id_ed25519_homebridge C:\Restore\Homebridge\homebridge.tar.gz <user>@<HOMEBRIDGE_HOST_IP>:/tmp/
ssh -t <user>@<HOMEBRIDGE_HOST_IP>
```
On the VM:
```bash
docker stop homebridge
cd /home/<user>/homelab/homebridge
sudo mv homebridge homebridge.old
sudo tar -xzf /tmp/homebridge.tar.gz -C /home/<user>/homelab/homebridge
docker start homebridge
```
On start, Homebridge reinstalls plugins (node_modules) from package.json, and the Apple TV plugin rebuilds its .venv.
Apple TV pairing (`appletv-enhanced/<id>`) and HomeKit pairing (`persist/`, `accessories/`) come back from the backup.
Once it works, remove `homebridge.old` and `/tmp/homebridge.tar.gz`.
