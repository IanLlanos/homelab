# Discord yt-dlp Bot

Anyone in the request channel can post a link. The bot looks it up and DMs each approver a preview (title, channel, length, thumbnail, estimated size) with **Approve** / **Deny**. Nothing downloads until an approver approves it and sets it up:

1. **Quality**: *Compatible* (1080p H.264 MP4, plays directly on Apple TV, Samsung TVs and browsers) or *Max quality* (up to 4K VP9/AV1 MKV).
2. **Destination**, with free space shown for each:
   - **General Downloads** (`C:\Users\<user>\Desktop\Downloads`)
   - **YouTube / Other Videos** (`X:\YouTube`, its own Plex library)
   - Any of the 10 PLEX drives, then **Movies** or **TV**
3. **Name**: a pop-up asks for the Plex name, prefilled from the video:
   - Movies: title and year, saved as `MOVIES\Title (Year)\Title (Year).mp4`
   - TV: show, season, episode, episode title, saved as `TV\Show\Season 01\Show - S01E02 - Title.mp4`
   - General / Other Videos: a file name

Downloads run one at a time. When one finishes, the bot tells Plex to scan just that folder so it shows up right away. The requester gets a DM when their request is approved, denied, finished, failed or cancelled.

## 1. Create the Discord bot

1. Go to https://discord.com/developers/applications and click **New Application**.
2. **Bot** tab: under **Privileged Gateway Intents**, turn on **Message Content Intent**.
3. Click **Reset Token** and copy the token for your `.env` file.
4. **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands` (needed for `/queue` and `/cancel`)
   - Bot permissions: `Send Messages`, `Read Message History`, `Embed Links`
   - Open the generated URL and invite the bot to your server.

Approvers need to share a server with the bot and allow DMs from server members, or the approval DM can't reach them.

## 2. Settings

Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`, `CHANNEL_ID`, `AUTHORIZED_USER_IDS` and `PLEX_TOKEN`. The optional settings (allowed sites, size limit, space margin, folder names) are listed in the same file.

## 3. Plex library for YouTube / Other Videos

Create the folder `X:\YouTube`, then in Plex add a library of type **Other Videos** pointing to it. To use a different folder, change `OTHER_VIDEOS_DIR` in `docker-compose.yml` (for example `/media/e/YouTube` for `E:\YouTube`).

## 4. Build and run

```
docker compose up -d --build
docker logs -f discord-ytdlp-bot
```

The log should end with `Logged in as …`. yt-dlp updates itself every time the container starts.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/queue` | Anyone | Lists requests waiting for approval, being set up, queued and downloading |
| `/cancel request:<number>` | Approvers | Cancels a request at any stage, including one that's downloading |

## Safety limits

- **Allowed sites only**: links from other sites are ignored with a short reply (`ALLOWED_DOMAINS`).
- **No playlists**: playlist links are rejected, and only single videos are downloaded.
- **Size limit**: anything over `MAX_FILESIZE_GB` (default 20 GB) is rejected.
- **Free-space check**: a destination can't be picked if the file would leave less than `SPACE_MARGIN_GB` (default 5 GB) free.
- **No duplicates**: every finished download is recorded in `bot-data/archive.txt`, and links already downloaded are skipped.

## Extras embedded in each file

Title and description metadata, chapters, the thumbnail as cover art, and English subtitles when available.

## Persistence

Everything is saved to `bot-data/state.json`. After a restart:

- **Waiting for approval**: approvers get a fresh DM, and the old one is marked as replaced.
- **Approved but not set up yet**: the approver gets a fresh DM starting at the quality step.
- **Queued or mid-download**: it goes back in line, and an interrupted download resumes from its partial file where possible.
