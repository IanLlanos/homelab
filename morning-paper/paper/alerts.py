"""Discord notices for failed or partial builds."""
import logging

import requests

log = logging.getLogger("paper.alerts")


def discord(cfg, title, lines, urgent=False):
    url = cfg.get("discord_webhook_url")
    if not url:
        return
    body = "\n".join(f"• {line}" for line in lines[:12])
    payload = {
        "username": cfg["paper_name"],
        "embeds": [{
            "title": title[:250],
            "description": body[:3900] or None,
            "color": 0xB03A2E if urgent else 0xC9A227,
        }],
    }
    try:
        requests.post(url, json=payload, timeout=15).raise_for_status()
    except Exception as exc:
        log.warning("Discord post failed: %s", exc)
