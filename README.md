# homelab

Scripts and containers that keep my Windows Plex server running hands-off.

| Project | What it does |
|---|---|
| [discord-ytdlp-bot](discord-ytdlp-bot/) | Discord bot that downloads approved video links with yt-dlp into the Plex libraries |
| [weekly-update](weekly-update/) | Weekly Windows Update run that waits for Plex streams to end and posts status to Discord |
| [transmission-turtle](transmission-turtle/) | Switches Transmission to turtle mode while anyone is streaming from Plex |
| [config-backup](config-backup/) | Weekly zip backup of all app configs (arr apps, Plex, Tautulli, Docker volumes, Homebridge) with Discord alerts |
| [drive-health](drive-health/) | Weekly SMART check of every drive with Discord alerts when something gets worse |
| [announcements](announcements/) | One-off scheduled @everyone Discord announcement with an image (Plex Wrapped) |
| [utilities](utilities/) | Small helpers: restart Plex if it crashed, start Claude minimized, remove duplicate media copies |

Every secret lives in a git-ignored `config.json` or `.env`. Copy the `*.example` file next to it and fill it in.
