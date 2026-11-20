"""Settings: built-in defaults, overridden by config.json."""
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_PATH = Path(os.environ.get("PAPER_CONFIG", "/app/config.json"))
DATA_DIR = Path(os.environ.get("PAPER_DATA", "/data"))
PACKAGE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "paper_name": "The Morning Ledger",
    "motto": "All the news that fits before coffee",
    "timezone": "America/Chicago",
    "build_time": "06:00",
    "retry_minutes": 15,
    "port": 8790,
    # Dallas is always in the weather and is the fallback location.
    "home": {"name": "Dallas", "state": "Texas", "lat": 32.7767, "lon": -96.7970},
    # Within this distance of home, the "current location" is treated as home.
    "home_radius_km": 60,
    # Shared secret for POST /api/location and POST /api/build.
    "token": "",
    "discord_webhook_url": "",
    # Feed errors in one build before Discord gets a warning.
    "feed_error_warn": 4,
    "retention_days": 30,
    "user_agent": "Mozilla/5.0 (compatible; morning-paper/1.0; personal homelab newspaper)",
    # Grayscale photos for the old-newspaper look. Set false for color.
    "grayscale_photos": True,
    "stories": {"politics": 8, "national": 8, "local": 6, "tech": 8, "investigations": 3},
    # Fetch the full article text for each story (story pages + EPUB).
    "full_text": True,
    "fetch_workers": 6,
    # Extra feeds to add, same shape as paper/sources.py entries.
    "extra_feeds": [],
    # Built-in feed names to leave out, e.g. ["Hacker News"].
    "disabled_feeds": [],
}


def _merge(base, extra):
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8-sig") as fh:
            cfg = _merge(cfg, json.load(fh))
    cfg["tz"] = ZoneInfo(cfg["timezone"])
    hh, mm = (int(x) for x in str(cfg["build_time"]).split(":"))
    cfg["build_hour"], cfg["build_minute"] = hh, mm
    return cfg


def data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
