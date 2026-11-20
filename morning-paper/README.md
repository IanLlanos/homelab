# morning-paper

A personal morning newspaper in the style of an old broadsheet. Every day at 6:00 AM Central it gathers national politics, national news, tech, long reads, local news for wherever your iPhone says you are, and weather for Dallas plus your current city. It prints the result as a web page for the iPad (and any browser on the LAN) and as an EPUB for an e-reader later.

- Paper: `http://192.168.1.118:8790`
- Past issues: `http://192.168.1.118:8790/archive`
- Today's EPUB: `http://192.168.1.118:8790/today.epub`
- Health for Uptime Kuma: `http://192.168.1.118:8790/health` (200 when today's paper exists, 503 when it's late)

## How it works

One Python container (`python:3.12-slim`, about 100 MB of RAM, capped at 256 MB). It serves the pages itself and runs its own daily schedule, so there's nothing in Task Scheduler.

- **Schedule:** builds at `build_time` (06:00). If the container was down then (the Monday 3 to 5 AM update and reboot), it builds as soon as it's back and retries every 15 minutes until today's issue exists. The very first start builds right away.
- **Alerts:** a failed build posts to the Discord webhook in `config.json`, once at the first failure and once more an hour later. A build where 4 or more sources failed posts a warning.
- **Sources** (`paper/sources.py`): NPR, PBS News, AP, ProPublica, Politico, The 19th, The Guardian, Axios, Reuters, The Marshall Project, Ars Technica, The Verge, Wired, 404 Media, Hacker News. Local: KERA, The Texas Tribune and Dallas Observer at home; Google News for your city when you're away. AP, Reuters and the local outlets have no usable RSS, so they come through Google News and are resolved back to the original article.
- **Storage:** the `morning-paper_paper-data` named volume holds 30 days of issues (a few MB a day) and your last location. Photos are not downloaded; the iPad loads them from the news sites.
- **Night mode:** the round button in the top right switches between day and night. Each device remembers its choice.
- **Styles:** add `?style=eink` to the URL for a black-and-white, no-photo, larger-type version (remembered on that device). `?style=color` switches back.

## Install (over SSH from Chungus, PowerShell 5.1)

1. Download the repo and copy this folder to `C:\Docker\morning-paper`:
   ```powershell
   Invoke-WebRequest https://github.com/IanLlanos/homelab/archive/refs/heads/main.zip -OutFile $env:TEMP\homelab.zip -UseBasicParsing
   Expand-Archive $env:TEMP\homelab.zip -DestinationPath $env:TEMP\homelab -Force
   New-Item -ItemType Directory C:\Docker\morning-paper -Force | Out-Null
   Copy-Item $env:TEMP\homelab\homelab-main\morning-paper\* C:\Docker\morning-paper -Recurse -Force
   ```
2. Create `config.json` with a new random token and your existing admin Discord webhook (nothing is printed):
   ```powershell
   $c = Get-Content C:\Docker\morning-paper\config.example.json -Raw | ConvertFrom-Json
   $c.token = -join ((1..40) | ForEach-Object { [char[]]'abcdefghijkmnpqrstuvwxyz23456789' | Get-Random })
   $c.discord_webhook_url = (Get-Content C:\Scripts\WeeklyUpdate\config.json -Raw | ConvertFrom-Json).WebhookUrl
   $c | ConvertTo-Json -Depth 5 | Set-Content C:\Docker\morning-paper\config.json -Encoding UTF8
   ```
3. Build and start. The empty `DOCKER_CONFIG` avoids the "A specified logon session does not exist" error over SSH:
   ```powershell
   $env:DOCKER_CONFIG = "$env:TEMP\dockercfg"
   New-Item -ItemType Directory $env:DOCKER_CONFIG -Force | Out-Null
   Set-Content "$env:DOCKER_CONFIG\config.json" '{"auths":{}}'
   Set-Location C:\Docker\morning-paper
   docker compose up -d --build
   ```
4. Check it (the first build takes a minute or two):
   ```powershell
   docker logs --tail 30 morning-paper
   Invoke-RestMethod http://192.168.1.118:8790/health
   ```
5. Optional: add `morning-paper_paper-data` to `ContainerVolumes` in `C:\Scripts\backuper\Backup-ServerConfig.ps1`. It only holds past issues, so skipping it is fine.
6. Optional: in Uptime Kuma add an HTTP monitor for `http://192.168.1.118:8790/health`.

Rebuild today's paper by hand: `docker exec morning-paper python -m paper build --force`

## Location from the iPhone

The paper only needs one location a day. The phone posts it to `/api/location` with the token.

**Public route (works at home and away).** In `C:\Docker\guest-portal\Caddyfile`, inside the `twistflix.ddns.net` site block and above the final catch-all `handle` (Wizarr), add:
```caddy
@paperloc {
	path /paper/location
	method POST
}
handle @paperloc {
	rewrite * /api/location
	reverse_proxy host.docker.internal:8790
}
```
Then reload: `docker exec guest-caddy caddy reload --config /etc/caddy/Caddyfile`. Only that one POST route is public. It needs the token, and the paper itself stays LAN-only.

**iPhone Shortcut** (Shortcuts app → Automation → New → Time of Day, 5:30 AM, Daily, **Run Immediately**):
1. **Get Current Location**
2. **Get Contents of URL**: `https://twistflix.ddns.net/paper/location`, Method **POST**, Header `Authorization` = `Bearer <your token>`, Request Body **JSON** with `lat` = *Current Location → Latitude* and `lon` = *Current Location → Longitude*.

Run it once by hand to allow location access. To see your token on the server: `(Get-Content C:\Docker\morning-paper\config.json -Raw | ConvertFrom-Json).token`

If no location has ever been posted, or you're within 60 km of Dallas, the paper uses Dallas.

## iPad

1. Open `http://192.168.1.118:8790` in Safari → Share → **Add to Home Screen**. It opens full screen like an app.
2. Shortcuts → Automation → New → **Time of Day** 6:30 AM daily (or **Charger → Is Disconnected**), **Run Immediately** → action **Open URLs** `http://192.168.1.118:8790`.

Layout: three columns in landscape, two in portrait.

Optional nicer address: add a Pi-hole local DNS record `paper.lan → 192.168.1.118` and this block to the guest-caddy Caddyfile, then use `http://paper.lan`:
```caddy
http://paper.lan {
	reverse_proxy host.docker.internal:8790
}
```

## Settings (config.json)

| Key | Default | What it does |
|---|---|---|
| `token` | none | Shared secret for `POST /api/location` and `POST /api/build`. Required for location updates. |
| `discord_webhook_url` | none | Where build failures go |
| `build_time` | `06:00` | Daily build time, Central |
| `paper_name`, `motto` | The Morning Ledger | Masthead text |
| `home` | Dallas | Always-on weather box and fallback location |
| `grayscale_photos` | `true` | Black-and-white photos for the old-paper look |
| `retention_days` | `30` | How many days of issues to keep |
| `stories` | politics 8, national 8, local 6, tech 8, long reads 3 | Stories per section |
| `disabled_feeds` | `[]` | Outlet names to leave out, e.g. `["Hacker News"]` |
| `extra_feeds` | `[]` | More feeds: `{"section": "tech", "name": "Example", "kind": "rss", "url": "https://..."}` |

## Development

```bash
pip install -r requirements.txt
python -m unittest discover tests     # offline; the network is faked
```

Fonts in `paper/static/fonts` (UnifrakturMaguntia, Playfair Display, Libre Caslon Text) are under the SIL Open Font License 1.1 and are bundled so the page loads nothing from font CDNs.
