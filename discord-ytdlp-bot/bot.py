"""
Discord yt-dlp download bot (approval-gated)

Flow:
  1. Someone posts a link in the request channel.
  2. The bot looks it up (title, channel, length, thumbnail, estimated size) and
     DMs every approver a preview with Approve / Deny.
  3. The approver picks a quality, then a destination (free space shown for each),
     then names it so Plex can match it (Movie / TV naming, or a plain file name).
  4. Downloads run one at a time. When one finishes, Plex is told to scan that folder.
  5. The requester gets a DM at each step. /queue shows what's going on, /cancel stops a request.

Everything is saved to STATE_FILE so a restart picks up where it left off.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
import copy

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from yt_dlp.utils import DownloadCancelled, make_archive_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytdlp-bot")


# --------------------------------------------------------------------------- settings
def _env_list(name, default=""):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0") or 0)
AUTHORIZED_USER_IDS = {int(x) for x in _env_list("AUTHORIZED_USER_IDS")}

STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
ARCHIVE_FILE = os.environ.get("ARCHIVE_FILE", "/data/archive.txt")

GENERAL_DOWNLOAD_DIR = os.environ.get("GENERAL_DOWNLOAD_DIR", "/downloads/general")
GENERAL_DOWNLOAD_HOST = os.environ.get("GENERAL_DOWNLOAD_HOST", r"C:\Users\<user>\Desktop\Downloads")
OTHER_VIDEOS_DIR = os.environ.get("OTHER_VIDEOS_DIR", "/media/x/YouTube")

MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/media").rstrip("/")
DRIVE_LETTERS = [d.strip().lower().rstrip(":") for d in _env_list("DRIVE_LETTERS", "D,E,F,G,H,I,J,K,L,X")]
MOVIES_FOLDER = os.environ.get("MOVIES_FOLDER", "MOVIES")
TV_FOLDER = os.environ.get("TV_FOLDER", "TV")

ALLOWED_DOMAINS = [d.lower() for d in _env_list(
    "ALLOWED_DOMAINS",
    "youtube.com,youtu.be,vimeo.com,twitter.com,x.com,reddit.com,redd.it,twitch.tv,"
    "tiktok.com,instagram.com,dailymotion.com,streamable.com",
)]
MAX_FILESIZE_GB = float(os.environ.get("MAX_FILESIZE_GB", "20") or 0)      # 0 = no limit
SPACE_MARGIN_GB = float(os.environ.get("SPACE_MARGIN_GB", "5") or 0)       # keep this much free on a drive

PLEX_URL = os.environ.get("PLEX_URL", "http://host.docker.internal:32400").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

GB = 1024 ** 3

QUALITIES = {
    "compatible": {
        "label": "Compatible: 1080p H.264 MP4",
        "desc": "Direct plays on Apple TV, Samsung TVs and browsers",
        "format": "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]/bv*[height<=1080]+ba/b",
        "merge": "mp4",
    },
    "max": {
        "label": "Max quality: up to 4K MKV",
        "desc": "VP9/AV1, some players may need Plex to transcode",
        "format": "bv*[height<=2160]+ba/b",
        "merge": "mkv",
    },
}

ACTIVE = ("pending", "configuring", "queued", "downloading")
FINAL = ("done", "failed", "denied", "cancelled")
COLORS = {"pending": 0xE3A008, "configuring": 0x5865F2, "queued": 0x5865F2, "downloading": 0x0A7A98,
          "done": 0x3FB950, "failed": 0xF0524A, "denied": 0x6E7681, "cancelled": 0x6E7681}

URL_RE = re.compile(r"https?://[^\s<>]+")


# --------------------------------------------------------------------------- helpers
def fmt_size(n):
    if not n:
        return "unknown"
    return f"{n / GB:.1f} GB" if n >= GB else f"{n / 1024 ** 2:.0f} MB"


def fmt_duration(s):
    if not s:
        return "unknown"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_name(s, fallback="video", limit=120):
    """Make a string safe as a Windows file/folder name."""
    s = _BAD_CHARS.sub("", s or "")
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    s = s[:limit].rstrip(". ")
    return s or fallback


def short_err(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()
    return msg[:300] or e.__class__.__name__


def domain_allowed(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def drive_path(letter):
    return f"{MEDIA_ROOT}/{letter}"


def drive_host(letter):
    return f"{letter.upper()}:\\"


def dest_dir(value):
    if value == "general":
        return GENERAL_DOWNLOAD_DIR
    if value == "other":
        return OTHER_VIDEOS_DIR
    return drive_path(value.split(":", 1)[1])


def dest_label(value):
    if value == "general":
        return "General Downloads"
    if value == "other":
        return "YouTube / Other Videos"
    return f"{value.split(':', 1)[1].upper()}: drive"


def free_bytes(path):
    """Free space for the drive holding `path`; None if that drive isn't mounted."""
    p = path.rstrip("/") or "/"
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    if p in ("/", MEDIA_ROOT):          # walked above the mount point: the drive isn't there
        return None
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def to_host_path(container_path):
    """Container path -> the Windows path Plex sees."""
    if not container_path:
        return None
    cp = container_path.replace("\\", "/")
    mapping = [(GENERAL_DOWNLOAD_DIR, GENERAL_DOWNLOAD_HOST)] + [(drive_path(l), drive_host(l)) for l in DRIVE_LETTERS]
    for c, h in sorted(mapping, key=lambda m: -len(m[0])):
        c = c.rstrip("/")
        if cp == c or cp.startswith(c + "/"):
            rest = cp[len(c):].strip("/")
            base = h.rstrip("\\")
            return base + "\\" + rest.replace("/", "\\") if rest else base + "\\"
    return None


def in_archive(archive_id):
    try:
        with open(ARCHIVE_FILE, encoding="utf-8") as f:
            return any(line.strip() == archive_id for line in f)
    except FileNotFoundError:
        return False


def estimate_size(info):
    fmts = info.get("requested_formats") or [info]
    dur = info.get("duration") or 0
    total = 0
    for f in fmts:
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and dur:
            size = f["tbr"] * 1000 / 8 * dur
        if not size:
            return None
        total += size
    return int(total)


# --------------------------------------------------------------------------- yt-dlp (runs in threads)
_BASE_OPTS = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}


def probe(url):
    """Look a link up without downloading. Returns the preview info."""
    with yt_dlp.YoutubeDL(_BASE_OPTS) as ydl:
        raw = ydl.extract_info(url, download=False, process=False)
    if not raw or raw.get("_type") in ("playlist", "multi_video"):
        raise ValueError("That link is a playlist. Post a link to a single video instead.")

    est, meta = {}, None
    for key, q in QUALITIES.items():
        opts = {**_BASE_OPTS, "format": q["format"]}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.process_ie_result(copy.deepcopy(raw), download=False)
        except Exception:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        if not info or info.get("_type") in ("playlist", "multi_video"):
            raise ValueError("That link is a playlist. Post a link to a single video instead.")
        est[key] = estimate_size(info)
        meta = meta or info

    upload = meta.get("upload_date") or ""
    extractor = meta.get("extractor_key") or meta.get("ie_key") or meta.get("extractor") or ""
    return {
        "title": meta.get("title") or url,
        "uploader": meta.get("uploader") or meta.get("channel") or "",
        "duration": meta.get("duration"),
        "thumbnail": meta.get("thumbnail"),
        "webpage_url": meta.get("webpage_url") or url,
        "year": str(meta.get("release_year") or (upload[:4] if len(upload) >= 4 else "")),
        "archive_id": make_archive_id(extractor, meta.get("id")) if extractor and meta.get("id") else None,
        "est": est,
    }


def run_download(job, cancel_event):
    folder, base = job["target_dir"], job["target_base"]
    os.makedirs(folder, exist_ok=True)
    q = QUALITIES[job["quality"]]

    def hook(_):
        if cancel_event.is_set():
            raise DownloadCancelled("Cancelled")

    opts = {
        "format": q["format"],
        "merge_output_format": q["merge"],
        "outtmpl": {"default": os.path.join(folder, base).replace("%", "%%") + ".%(ext)s"},
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "windowsfilenames": True,
        "overwrites": False,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "writethumbnail": True,
        "writesubtitles": True,
        "subtitleslangs": ["en.*", "en"],
        "download_archive": ARCHIVE_FILE,
        "progress_hooks": [hook],
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False},
            {"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ],
    }
    if MAX_FILESIZE_GB > 0:
        opts["max_filesize"] = int(MAX_FILESIZE_GB * GB)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(job["url"], download=True)
    if not info:
        raise RuntimeError("yt-dlp skipped it (it's already in the download archive)")
    downloads = info.get("requested_downloads") or []
    path = downloads[-1].get("filepath") if downloads else None
    if not path or not os.path.exists(path):
        raise RuntimeError("yt-dlp finished but no file was saved (it may be over the size limit)")
    return path


def cleanup_partials(job):
    folder, base = job.get("target_dir"), job.get("target_base")
    if not folder or not base or not os.path.isdir(folder):
        return
    for f in os.listdir(folder):
        if f.startswith(base + ".") and (".part" in f or f.endswith((".ytdl", ".temp", ".jpg", ".webp", ".vtt", ".srt"))):
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass


def build_target(job):
    """Folder + file name (no extension) following Plex naming."""
    d, n = job["dest"], job["naming"]
    if d["kind"] == "drive":
        root = drive_path(d["drive"])
        if d["category"] == "movies":
            name = f'{clean_name(n["title"], "Movie", 100)} ({n["year"]})'
            folder, base = os.path.join(root, MOVIES_FOLDER, name), name
        else:
            show = clean_name(n["show"], "Show", 100)
            s, e = int(n["season"]), int(n["episode"])
            folder = os.path.join(root, TV_FOLDER, show, f"Season {s:02d}")
            base = f"{show} - S{s:02d}E{e:02d}"
            if n.get("episode_title"):
                base += f" - {clean_name(n['episode_title'], '', 100)}".rstrip(" -")
    else:
        folder = GENERAL_DOWNLOAD_DIR if d["kind"] == "general" else OTHER_VIDEOS_DIR
        base = clean_name(n["name"])

    existing = os.listdir(folder) if os.path.isdir(folder) else []
    candidate, i = base, 2
    while any(f.startswith(candidate + ".") for f in existing):
        candidate, i = f"{base} ({i})", i + 1
    return folder, candidate


def plex_refresh(host_dir):
    """Ask Plex to scan just this folder in whichever library covers it."""
    if not PLEX_TOKEN:
        return "Plex scan skipped (no PLEX_TOKEN set)"
    if not host_dir:
        return "Plex scan skipped"
    tok = urllib.parse.quote(PLEX_TOKEN)
    req = urllib.request.Request(f"{PLEX_URL}/library/sections?X-Plex-Token={tok}",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    target = host_dir.replace("/", "\\").rstrip("\\").lower()
    best = None
    for d in data.get("MediaContainer", {}).get("Directory", []):
        for loc in d.get("Location", []):
            p = (loc.get("path") or "").replace("/", "\\").rstrip("\\").lower()
            if p and (target == p or target.startswith(p + "\\")) and (not best or len(p) > best[1]):
                best = (d.get("key"), len(p), d.get("title"))
    if not best:
        return "no Plex library covers that folder, so nothing to scan"
    url = (f"{PLEX_URL}/library/sections/{best[0]}/refresh?"
           f"path={urllib.parse.quote(host_dir)}&X-Plex-Token={tok}")
    urllib.request.urlopen(urllib.request.Request(url), timeout=15).close()
    return f"Plex is scanning it into “{best[2]}”"


# --------------------------------------------------------------------------- state
jobs = {}            # id (str) -> job dict
_counter = 0
download_queue = asyncio.Queue()
cancel_events = {}   # id -> threading.Event for the running download


def load_state():
    global jobs, _counter
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        jobs = {str(k): v for k, v in data.get("jobs", {}).items()}
        _counter = int(data.get("counter", 0))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("Couldn't read %s (%s), starting fresh", STATE_FILE, e)


def save_state():
    finished = sorted((j for j in jobs.values() if j["status"] in FINAL), key=lambda j: j.get("finished", 0))
    for j in finished[:-50]:                       # keep the last 50 finished requests
        jobs.pop(j["id"], None)
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"counter": _counter, "jobs": jobs}, f, indent=2)
    os.replace(tmp, STATE_FILE)


def next_id():
    global _counter
    _counter += 1
    return str(_counter)


def queue_position(job):
    queued = sorted((j for j in jobs.values() if j["status"] == "queued"), key=lambda j: j.get("queued_at", 0))
    for i, j in enumerate(queued, 1):
        if j["id"] == job["id"]:
            return i
    return None


def est_for(job):
    return job["info"]["est"].get(job.get("quality") or "compatible")


def save_to_text(job):
    if job.get("result_path"):
        return to_host_path(job["result_path"]) or job["result_path"]
    if job.get("target_dir"):
        ext = QUALITIES[job["quality"]]["merge"] if job.get("quality") else "mp4"
        return to_host_path(os.path.join(job["target_dir"], job["target_base"])) + f".{ext}"
    d = job.get("dest")
    if not d:
        return None
    if d["kind"] == "drive":
        cat = {"movies": "Movies", "tv": "TV"}.get(d.get("category"), "…")
        return f"{d['drive'].upper()}: drive › {cat}"
    return dest_label(d["kind"])


# --------------------------------------------------------------------------- discord
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
NO_PINGS = discord.AllowedMentions.none()


def job_embed(job, stage=None):
    i = job["info"]
    e = discord.Embed(title=i["title"][:256], url=i["webpage_url"], color=COLORS.get(job["status"], 0x5865F2))
    e.add_field(name="Requested by", value=f"<@{job['requester_id']}>")
    if i.get("uploader"):
        e.add_field(name="Channel", value=i["uploader"][:1024])
    e.add_field(name="Length", value=fmt_duration(i.get("duration")))
    est = i.get("est", {})
    e.add_field(name="Estimated size",
                value=f"Compatible: ~{fmt_size(est.get('compatible'))}\nMax quality: ~{fmt_size(est.get('max'))}",
                inline=False)
    if job.get("quality"):
        e.add_field(name="Quality", value=QUALITIES[job["quality"]]["label"], inline=False)
    where = save_to_text(job)
    if where:
        e.add_field(name="Save to", value=f"`{where}`", inline=False)
    if job.get("error"):
        e.add_field(name="Error", value=job["error"][:1024], inline=False)
    if i.get("thumbnail"):
        e.set_thumbnail(url=i["thumbnail"])
    e.set_footer(text=f"Request #{job['id']}" + (f" · {stage}" if stage else ""))
    return e


def channel_text(job):
    i = job["info"]
    s = job["status"]
    if s == "pending":
        line = "⏳ Waiting for approval"
    elif s == "configuring":
        line = f"🛠️ Approved by {job.get('approver_name', 'an approver')}, picking where to save it"
    elif s == "queued":
        line = f"📥 Queued (#{queue_position(job)} in line) → `{save_to_text(job)}`"
    elif s == "downloading":
        line = f"⬇️ Downloading → `{save_to_text(job)}`"
    elif s == "done":
        line = f"✅ Done → `{save_to_text(job)}`" + (f"\n{job['plex_scan']}" if job.get("plex_scan") else "")
    elif s == "failed":
        line = f"❌ Failed: {job.get('error', 'unknown error')}"
    elif s == "denied":
        line = f"🚫 Denied by {job.get('approver_name', 'an approver')}"
    else:
        line = "🛑 Cancelled"
    return f"**#{job['id']}** {line}\n**{i['title'][:200]}** (requested by <@{job['requester_id']}>)\n<{i['webpage_url']}>"


async def update_channel(job):
    ch = bot.get_channel(CHANNEL_ID)
    if not ch or not job.get("channel_msg_id"):
        return
    try:
        await ch.get_partial_message(job["channel_msg_id"]).edit(content=channel_text(job), allowed_mentions=NO_PINGS)
    except discord.HTTPException as e:
        log.warning("Couldn't update channel message for #%s: %s", job["id"], e)


async def get_user(uid):
    return bot.get_user(uid) or await bot.fetch_user(uid)


async def edit_dm(uid, mid, **kwargs):
    try:
        user = await get_user(uid)
        dm = user.dm_channel or await user.create_dm()
        await dm.get_partial_message(mid).edit(**kwargs)
    except discord.HTTPException as e:
        log.warning("Couldn't edit DM %s for %s: %s", mid, uid, e)


async def notify_requester(job, text):
    try:
        user = await get_user(job["requester_id"])
        await user.send(text)
    except discord.HTTPException:
        pass  # requester has DMs closed; the channel message still shows the status


async def close_approval_dms(job, text, except_mid=None):
    for uid, mid in job.get("dm_messages", []):
        if mid != except_mid:
            await edit_dm(uid, mid, content=text, embed=job_embed(job), view=None)


# ---- approval ------------------------------------------------------------------
class ApprovalView(discord.ui.View):
    def __init__(self, job_id):
        super().__init__(timeout=None)
        self.job_id = job_id

    async def interaction_check(self, interaction):
        if interaction.user.id not in AUTHORIZED_USER_IDS:
            await interaction.response.send_message("Only approvers can do that.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction, _button):
        job = jobs.get(self.job_id)
        if not job or job["status"] != "pending":
            await interaction.response.edit_message(content="This request was already handled.", view=None)
            return
        job.update(status="configuring", approver_id=interaction.user.id,
                   approver_name=interaction.user.display_name,
                   config_dm=[interaction.user.id, interaction.message.id])
        save_state()
        await interaction.response.edit_message(content="Pick a quality:", embed=job_embed(job, "Step 1 of 3: quality"),
                                                view=QualityView(self.job_id))
        await close_approval_dms(job, f"✅ Approved by {interaction.user.display_name}, who is picking where to save it.",
                                 except_mid=interaction.message.id)
        await update_channel(job)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="✖️")
    async def deny(self, interaction, _button):
        job = jobs.get(self.job_id)
        if not job or job["status"] != "pending":
            await interaction.response.edit_message(content="This request was already handled.", view=None)
            return
        job.update(status="denied", approver_id=interaction.user.id,
                   approver_name=interaction.user.display_name, finished=time.time())
        save_state()
        await interaction.response.edit_message(content="🚫 Denied.", embed=job_embed(job), view=None)
        await close_approval_dms(job, f"🚫 Denied by {interaction.user.display_name}.", except_mid=interaction.message.id)
        await update_channel(job)
        await notify_requester(job, f"🚫 Your request **{job['info']['title'][:200]}** was denied.")


async def send_approval_dms(job):
    job["dm_messages"] = []
    for uid in AUTHORIZED_USER_IDS:
        try:
            user = await get_user(uid)
            msg = await user.send(content="📨 New download request:", embed=job_embed(job, "Waiting for approval"),
                                  view=ApprovalView(job["id"]))
            job["dm_messages"].append([uid, msg.id])
        except discord.HTTPException:
            ch = bot.get_channel(CHANNEL_ID)
            if ch:
                await ch.send(f"⚠️ Couldn't DM <@{uid}> for approval (DMs may be closed).")
    save_state()


# ---- approver set-up: quality -> destination -> name ----------------------------
class ConfigView(discord.ui.View):
    def __init__(self, job_id):
        super().__init__(timeout=None)
        self.job_id = job_id

    async def interaction_check(self, interaction):
        job = jobs.get(self.job_id)
        if not job or job["status"] != "configuring":
            await interaction.response.edit_message(content="This request isn't waiting on you anymore.", view=None)
            return False
        if interaction.user.id != job.get("approver_id"):
            await interaction.response.send_message("Only the approver who accepted this can set it up.", ephemeral=True)
            return False
        return True


class QualityView(ConfigView):
    def __init__(self, job_id):
        super().__init__(job_id)
        job = jobs[job_id]
        options = []
        for key, q in QUALITIES.items():
            est = job["info"]["est"].get(key)
            too_big = bool(est and MAX_FILESIZE_GB > 0 and est > MAX_FILESIZE_GB * GB)
            desc = f"~{fmt_size(est)} · {q['desc']}" + (" · over the size limit" if too_big else "")
            options.append(discord.SelectOption(label=q["label"], value=key, description=desc[:100],
                                                emoji="📺" if key == "compatible" else "🎞️"))
        self.select = discord.ui.Select(placeholder="Choose a quality…", options=options)
        self.select.callback = self.picked
        self.add_item(self.select)

    async def picked(self, interaction):
        job = jobs[self.job_id]
        key = self.select.values[0]
        est = job["info"]["est"].get(key)
        if est and MAX_FILESIZE_GB > 0 and est > MAX_FILESIZE_GB * GB:
            await interaction.response.send_message(
                f"That one is ~{fmt_size(est)}, over the {MAX_FILESIZE_GB:g} GB limit. Pick the other quality.", ephemeral=True)
            return
        job["quality"] = key
        save_state()
        await interaction.response.edit_message(content="Pick where to save it:",
                                                embed=job_embed(job, "Step 2 of 3: destination"),
                                                view=await DestinationView.create(self.job_id))


class DestinationView(ConfigView):
    @classmethod
    async def create(cls, job_id):
        values = ["general", "other"] + [f"drive:{l}" for l in DRIVE_LETTERS]
        frees = await asyncio.gather(*(asyncio.to_thread(free_bytes, dest_dir(v)) for v in values))
        return cls(job_id, dict(zip(values, frees)))

    def __init__(self, job_id, frees):
        super().__init__(job_id)
        self.frees = frees
        need = est_for(jobs[job_id]) or 0
        options = []
        for value, free in frees.items():
            if free is None:
                desc = "not available (check the volume mounts)"
            else:
                desc = f"{fmt_size(free)} free"
                if free - need < SPACE_MARGIN_GB * GB:
                    desc += " · not enough space"
            emoji = "📂" if value == "general" else "▶️" if value == "other" else "💽"
            options.append(discord.SelectOption(label=dest_label(value), value=value, description=desc[:100], emoji=emoji))
        self.select = discord.ui.Select(placeholder="Choose a destination…", options=options)
        self.select.callback = self.picked
        self.add_item(self.select)

    async def picked(self, interaction):
        job = jobs[self.job_id]
        value = self.select.values[0]
        free = await asyncio.to_thread(free_bytes, dest_dir(value))
        need = est_for(job) or 0
        if free is None:
            await interaction.response.send_message(f"{dest_label(value)} isn't available to the bot. Pick another.", ephemeral=True)
            return
        if free - need < SPACE_MARGIN_GB * GB:
            await interaction.response.send_message(
                f"Not enough room on {dest_label(value)}: it needs ~{fmt_size(need)} and only {fmt_size(free)} is free "
                f"(keeping {SPACE_MARGIN_GB:g} GB spare). Pick another.", ephemeral=True)
            return
        if value.startswith("drive:"):
            job["dest"] = {"kind": "drive", "drive": value.split(":", 1)[1]}
            save_state()
            await interaction.response.edit_message(content="Movies or TV?", embed=job_embed(job, "Step 2 of 3: destination"),
                                                    view=CategoryView(self.job_id))
        else:
            job["dest"] = {"kind": value}
            save_state()
            await interaction.response.send_modal(FileNameModal(self.job_id))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction, _button):
        job = jobs[self.job_id]
        await interaction.response.edit_message(content="Pick a quality:", embed=job_embed(job, "Step 1 of 3: quality"),
                                                view=QualityView(self.job_id))


class CategoryView(ConfigView):
    @discord.ui.button(label="Movies", style=discord.ButtonStyle.primary, emoji="🎬")
    async def movies(self, interaction, _button):
        jobs[self.job_id]["dest"]["category"] = "movies"
        await interaction.response.send_modal(MovieModal(self.job_id))

    @discord.ui.button(label="TV", style=discord.ButtonStyle.primary, emoji="📺")
    async def tv(self, interaction, _button):
        jobs[self.job_id]["dest"]["category"] = "tv"
        await interaction.response.send_modal(TVModal(self.job_id))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, _button):
        job = jobs[self.job_id]
        await interaction.response.edit_message(content="Pick where to save it:",
                                                embed=job_embed(job, "Step 2 of 3: destination"),
                                                view=await DestinationView.create(self.job_id))


class MovieModal(discord.ui.Modal):
    def __init__(self, job_id):
        super().__init__(title="Name it for Plex (Movie)", timeout=900)
        self.job_id = job_id
        info = jobs[job_id]["info"]
        self.name = discord.ui.TextInput(label="Movie title", default=clean_name(info["title"], "", 100)[:100], max_length=100)
        self.year = discord.ui.TextInput(label="Year", default=info.get("year") or None, min_length=4, max_length=4)
        self.add_item(self.name)
        self.add_item(self.year)

    async def on_submit(self, interaction):
        if not self.year.value.isdigit():
            await interaction.response.send_message("Year has to be 4 digits, e.g. 2024. Click Movies again.", ephemeral=True)
            return
        await finalize(interaction, self.job_id, {"title": self.name.value, "year": self.year.value})


class TVModal(discord.ui.Modal):
    def __init__(self, job_id):
        super().__init__(title="Name it for Plex (TV)", timeout=900)
        self.job_id = job_id
        info = jobs[job_id]["info"]
        self.show = discord.ui.TextInput(label="Show name", default=clean_name(info.get("uploader"), "", 100)[:100] or None,
                                         max_length=100)
        self.season = discord.ui.TextInput(label="Season number", placeholder="1", max_length=3)
        self.episode = discord.ui.TextInput(label="Episode number", placeholder="1", max_length=4)
        self.ep_title = discord.ui.TextInput(label="Episode title (optional)", required=False,
                                             default=clean_name(info["title"], "", 100)[:100] or None, max_length=100)
        for item in (self.show, self.season, self.episode, self.ep_title):
            self.add_item(item)

    async def on_submit(self, interaction):
        if not (self.season.value.strip().isdigit() and self.episode.value.strip().isdigit()):
            await interaction.response.send_message("Season and episode have to be numbers. Click TV again.", ephemeral=True)
            return
        await finalize(interaction, self.job_id, {
            "show": self.show.value, "season": self.season.value.strip(),
            "episode": self.episode.value.strip(), "episode_title": self.ep_title.value.strip(),
        })


class FileNameModal(discord.ui.Modal):
    def __init__(self, job_id):
        super().__init__(title="File name", timeout=900)
        self.job_id = job_id
        info = jobs[job_id]["info"]
        self.name = discord.ui.TextInput(label="File name (no extension)",
                                         default=clean_name(info["title"], "", 100)[:100] or None, max_length=100)
        self.add_item(self.name)

    async def on_submit(self, interaction):
        await finalize(interaction, self.job_id, {"name": self.name.value})


async def finalize(interaction, job_id, naming):
    job = jobs.get(job_id)
    if not job or job["status"] != "configuring":
        await interaction.response.send_message("This request isn't waiting on you anymore.", ephemeral=True)
        return
    await interaction.response.defer()
    job["naming"] = naming
    try:
        job["target_dir"], job["target_base"] = await asyncio.to_thread(build_target, job)
    except Exception as e:
        await interaction.followup.send(f"Couldn't use that name: {short_err(e)}", ephemeral=True)
        return
    job.update(status="queued", queued_at=time.time())
    save_state()
    await download_queue.put(job_id)
    pos = queue_position(job)
    await interaction.edit_original_response(content=f"📥 Queued (#{pos} in line).",
                                             embed=job_embed(job, "Queued"), view=None)
    await update_channel(job)
    await notify_requester(job, f"✅ Your request **{job['info']['title'][:200]}** was approved and is #{pos} in line.")


# ---- download worker ---------------------------------------------------------
async def worker():
    while True:
        job_id = await download_queue.get()
        job = jobs.get(job_id)
        if not job or job["status"] != "queued":
            continue
        job.update(status="downloading", started=time.time(), error=None)
        save_state()
        await update_channel(job)
        ev = threading.Event()
        cancel_events[job_id] = ev
        try:
            path = await asyncio.to_thread(run_download, job, ev)
            job.update(status="done", result_path=path)
            host = to_host_path(path)
            try:
                job["plex_scan"] = await asyncio.to_thread(plex_refresh, host.rsplit("\\", 1)[0] if host else None)
            except Exception as e:
                job["plex_scan"] = f"Plex scan failed: {short_err(e)}"
        except Exception as e:
            if ev.is_set() or isinstance(e, DownloadCancelled):
                job["status"] = "cancelled"
            else:
                job.update(status="failed", error=short_err(e))
            await asyncio.to_thread(cleanup_partials, job)
        finally:
            cancel_events.pop(job_id, None)
            job["finished"] = time.time()
            save_state()

        await update_channel(job)
        if job.get("config_dm"):
            await edit_dm(*job["config_dm"], content=None, embed=job_embed(job, job["status"].capitalize()), view=None)
        title = job["info"]["title"][:200]
        if job["status"] == "done":
            await notify_requester(job, f"✅ **{title}** finished downloading and is saved to `{save_to_text(job)}`.")
        elif job["status"] == "failed":
            await notify_requester(job, f"❌ **{title}** failed to download: {job['error']}")
        else:
            await notify_requester(job, f"🛑 **{title}** was cancelled.")
        # other queued items moved up a spot
        for j in jobs.values():
            if j["status"] == "queued":
                await update_channel(j)


# ---- requests ----------------------------------------------------------------
async def create_request(message, url):
    status = await message.reply("🔎 Looking up that link…", mention_author=False)
    try:
        info = await asyncio.to_thread(probe, url)
    except Exception as e:
        await status.edit(content=f"❌ Couldn't read that link: {short_err(e)}")
        return
    if info.get("archive_id") and in_archive(info["archive_id"]):
        await status.edit(content=f"♻️ **{info['title'][:200]}** was already downloaded before, so I'm skipping it.")
        return
    sizes = [s for s in info["est"].values() if s]
    if MAX_FILESIZE_GB > 0 and sizes and min(sizes) > MAX_FILESIZE_GB * GB:
        await status.edit(content=f"📦 **{info['title'][:200]}** is too large (~{fmt_size(min(sizes))}). "
                                  f"The limit is {MAX_FILESIZE_GB:g} GB.")
        return
    job = {"id": next_id(), "url": url, "info": info, "status": "pending",
           "requester_id": message.author.id, "requester_name": message.author.display_name,
           "channel_msg_id": status.id, "created": time.time()}
    jobs[job["id"]] = job
    save_state()
    await update_channel(job)
    await send_approval_dms(job)


@bot.event
async def on_message(message):
    if message.author.bot or message.channel.id != CHANNEL_ID:
        return
    for url in URL_RE.findall(message.content)[:5]:
        url = url.rstrip(">)].,!")
        if not domain_allowed(url):
            host = urllib.parse.urlparse(url).hostname or url
            await message.reply(f"⚠️ `{host}` isn't on the allowed sites list, so I'm skipping it.", mention_author=False)
            continue
        await create_request(message, url)
    await bot.process_commands(message)


# ---- slash commands ----------------------------------------------------------
STATE_ICON = {"pending": "⏳", "configuring": "🛠️", "queued": "📥", "downloading": "⬇️"}


@bot.tree.command(name="queue", description="Show download requests that are waiting or in progress")
async def queue_cmd(interaction: discord.Interaction):
    order = {"downloading": 0, "queued": 1, "configuring": 2, "pending": 3}
    active = sorted((j for j in jobs.values() if j["status"] in ACTIVE),
                    key=lambda j: (order[j["status"]], j.get("queued_at") or j.get("created", 0)))
    if not active:
        await interaction.response.send_message("Nothing in the queue right now.", ephemeral=True)
        return
    lines = []
    for j in active:
        label = {"pending": "waiting for approval", "configuring": "approved, being set up",
                 "queued": f"#{queue_position(j)} in line", "downloading": "downloading now"}[j["status"]]
        lines.append(f"{STATE_ICON[j['status']]} `#{j['id']}` **{j['info']['title'][:80]}**: {label}")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@bot.tree.command(name="cancel", description="Cancel a download request (approvers only)")
@app_commands.describe(request="The request number, e.g. 12")
async def cancel_cmd(interaction: discord.Interaction, request: int):
    if interaction.user.id not in AUTHORIZED_USER_IDS:
        await interaction.response.send_message("Only approvers can cancel requests.", ephemeral=True)
        return
    job = jobs.get(str(request))
    if not job or job["status"] not in ACTIVE:
        await interaction.response.send_message(f"Request #{request} isn't active.", ephemeral=True)
        return
    if job["status"] == "downloading":
        ev = cancel_events.get(job["id"])
        if ev:
            ev.set()
        await interaction.response.send_message(f"Stopping #{request}…", ephemeral=True)
        return
    job.update(status="cancelled", finished=time.time())
    save_state()
    await interaction.response.send_message(f"Cancelled #{request}.", ephemeral=True)
    await close_approval_dms(job, f"🛑 Cancelled by {interaction.user.display_name}.")
    if job.get("config_dm"):
        await edit_dm(*job["config_dm"], content=f"🛑 Cancelled by {interaction.user.display_name}.",
                      embed=job_embed(job), view=None)
    await update_channel(job)
    await notify_requester(job, f"🛑 Your request **{job['info']['title'][:200]}** was cancelled.")


# ---- startup -----------------------------------------------------------------
async def resume_jobs():
    restart_note = "↻ The bot restarted. Use the new message below."
    waiting = sorted((j for j in jobs.values() if j["status"] in ("downloading", "queued")),
                     key=lambda j: (j["status"] != "downloading", j.get("queued_at", 0)))
    for j in waiting:
        j["status"] = "queued"
        await download_queue.put(j["id"])
    for j in list(jobs.values()):
        if j["status"] == "pending":
            for uid, mid in j.get("dm_messages", []):
                await edit_dm(uid, mid, content=restart_note, view=None)
            await send_approval_dms(j)
        elif j["status"] == "configuring":
            if j.get("config_dm"):
                await edit_dm(*j["config_dm"], content=restart_note, view=None)
            try:
                user = await get_user(j["approver_id"])
                msg = await user.send(content="↻ Picking up where you left off. Pick a quality:",
                                      embed=job_embed(j, "Step 1 of 3: quality"), view=QualityView(j["id"]))
                j["config_dm"] = [j["approver_id"], msg.id]
            except discord.HTTPException as e:
                log.warning("Couldn't resume set-up DM for #%s: %s", j["id"], e)
        if j["status"] in ACTIVE:
            await update_channel(j)
    save_state()


_started = False


@bot.event
async def on_ready():
    global _started
    log.info("Logged in as %s, watching channel %s", bot.user, CHANNEL_ID)
    if _started:
        return
    _started = True
    ch = bot.get_channel(CHANNEL_ID)
    if ch is None:
        log.error("Can't see channel %s. Check CHANNEL_ID and that the bot is in that server.", CHANNEL_ID)
    else:
        bot.tree.copy_global_to(guild=ch.guild)
        await bot.tree.sync(guild=ch.guild)
    await resume_jobs()
    asyncio.create_task(worker())


def main():
    missing = [n for n, v in (("DISCORD_TOKEN", DISCORD_TOKEN), ("CHANNEL_ID", CHANNEL_ID),
                              ("AUTHORIZED_USER_IDS", AUTHORIZED_USER_IDS)) if not v]
    if missing:
        raise SystemExit(f"Missing settings in .env: {', '.join(missing)}")
    load_state()
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
