"""Builds the paper at build_time each day. If the server was down at that
time (the Monday update reboot), it builds as soon as it's back, and retries
every retry_minutes until today's issue exists."""
import logging
import threading
import time
from datetime import datetime, timedelta

from . import alerts, build

log = logging.getLogger("paper.scheduler")


def due(cfg, now=None):
    now = now or datetime.now(cfg["tz"])
    start = now.replace(hour=cfg["build_hour"], minute=cfg["build_minute"], second=0, microsecond=0)
    return now >= start


def expected_date(cfg, now=None):
    """The newest issue that should exist right now."""
    now = now or datetime.now(cfg["tz"])
    # Allow an hour for the build and its retries before calling today's issue late.
    grace = now - timedelta(hours=1)
    return now.date() if due(cfg, grace) and grace.date() == now.date() else now.date() - timedelta(days=1)


def run_once(cfg, state):
    now = datetime.now(cfg["tz"])
    # The very first start builds right away, so there's something to look at.
    if not due(cfg, now) and build.issue_dates():
        return
    today = now.date()
    path = build.issues_dir() / today.isoformat() / "index.html"
    if path.exists():
        return
    last = state.get("last_attempt")
    if last and now - last < timedelta(minutes=cfg["retry_minutes"]):
        return
    state["last_attempt"] = now
    try:
        result = build.build(cfg, today)
    except Exception as exc:
        log.exception("Build failed")
        state["failures"] = state.get("failures", 0) + 1
        if state["failures"] in (1, 4):  # first failure, then once more an hour later
            alerts.discord(cfg, f"Morning paper build failed ({today})",
                           [str(exc), f"Retrying every {cfg['retry_minutes']} minutes."], urgent=True)
        return
    state["failures"] = 0
    if result.get("status") == "built":
        errs = result.get("feed_errors", []) + result.get("weather_errors", [])
        if len(errs) >= cfg["feed_error_warn"]:
            alerts.discord(cfg, f"Morning paper built with {len(errs)} source errors", errs)


def loop(cfg, stop):
    state = {}
    while not stop.is_set():
        try:
            run_once(cfg, state)
        except Exception:
            log.exception("Scheduler tick failed")
        stop.wait(60)


def start(cfg):
    stop = threading.Event()
    thread = threading.Thread(target=loop, args=(cfg, stop), name="scheduler", daemon=True)
    thread.start()
    return stop
