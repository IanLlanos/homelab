"""Build one day's paper: gather, choose, render HTML + EPUB, save."""
import json
import logging
import shutil
import threading
from datetime import date, datetime, timedelta, timezone

from . import location, news, render, weather
from .settings import data_dir
from .sources import SECTIONS, all_feeds

log = logging.getLogger("paper.build")
_lock = threading.Lock()


def issues_dir():
    d = data_dir() / "issues"
    d.mkdir(exist_ok=True)
    return d


def issue_dates():
    """All built issue dates, newest first."""
    out = []
    for p in issues_dir().iterdir():
        if (p / "index.html").exists():
            try:
                out.append(date.fromisoformat(p.name))
            except ValueError:
                pass
    return sorted(out, reverse=True)


def _meta():
    path = data_dir() / "meta.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_meta(meta):
    (data_dir() / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def build(cfg, day=None, force=False):
    """Returns a result dict. Raises only on a total failure."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _build(cfg, day, force)
    finally:
        _lock.release()


def _build(cfg, day, force):
    now = datetime.now(cfg["tz"])
    day = day or now.date()
    out_dir = issues_dir() / day.isoformat()
    if (out_dir / "index.html").exists() and not force:
        return {"status": "exists", "date": day.isoformat()}

    place, near_home = location.current(cfg)
    if place.get("updated") and not (place.get("city") or place.get("state")):
        # The geocoder was down when the location arrived; try once more.
        try:
            location.save(place["lat"], place["lon"], cfg)
            place, near_home = location.current(cfg)
        except Exception:
            log.warning("Reverse geocoding still failing")
    home = cfg["home"]

    # Weather: Dallas always, plus where you are when you're away.
    boxes = [weather.report(home["name"], home["lat"], home["lon"], cfg, us=True)]
    if not near_home:
        boxes.append(weather.report(location.label(place), place["lat"], place["lon"], cfg,
                                    us=(place.get("country_code") in ("US", "", None))))
    weather_errors = [e for b in boxes for e in b["errors"]]

    feeds = all_feeds(cfg, place, near_home)
    items, feed_errors = news.gather(feeds, cfg, now.astimezone(timezone.utc))
    order = [key for key, _ in SECTIONS]
    spare = cfg["spare_stories"] if cfg["full_text"] else 0
    chosen = news.pick(items, order, {k: v + spare for k, v in cfg["stories"].items()})
    text_errors = news.add_full_text(chosen, cfg)
    chosen = news.prefer_readable(chosen, cfg["stories"])

    total = sum(len(v) for v in chosen.values())
    if total == 0:
        raise RuntimeError("No stories at all. Feed errors: " + "; ".join(feed_errors[:6]))

    meta = _meta()
    first = date.fromisoformat(meta.get("first_issue", day.isoformat()))
    meta.setdefault("first_issue", first.isoformat())

    issue = {
        "date": day.isoformat(),
        "built": now.isoformat(timespec="seconds"),
        "number": (day - first).days + 1,
        "volume": day.year - first.year + 1,
        "place": place,
        "place_label": location.label(place),
        "near_home": near_home,
        "weather": boxes,
        "sections": [
            {"key": key, "title": title, "stories": chosen.get(key, [])}
            for key, title in SECTIONS
        ],
        "errors": {"feeds": feed_errors, "weather": weather_errors, "text": text_errors},
    }
    # Number every story so story pages have stable addresses.
    n = 0
    for sec in issue["sections"]:
        for story in sec["stories"]:
            n += 1
            story["id"] = n

    tmp_dir = out_dir.with_name(out_dir.name + ".tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    (tmp_dir / "issue.json").write_text(json.dumps(issue, indent=1), encoding="utf-8")
    render.write_issue(issue, cfg, tmp_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    tmp_dir.rename(out_dir)

    meta["last_built"] = issue["built"]
    _save_meta(meta)
    _prune(cfg)
    render.write_archive(cfg, issue_dates())
    log.info("Built %s: %d stories, %d feed errors", day, total, len(feed_errors))
    return {"status": "built", "date": day.isoformat(), "stories": total,
            "feed_errors": feed_errors, "weather_errors": weather_errors}


def _prune(cfg):
    keep = cfg["retention_days"]
    if not keep:
        return
    cutoff = datetime.now(cfg["tz"]).date() - timedelta(days=keep)
    for p in issues_dir().iterdir():
        try:
            if date.fromisoformat(p.name.replace(".tmp", "")) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except ValueError:
            pass
