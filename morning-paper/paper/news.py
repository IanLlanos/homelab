"""Fetch feeds, pick the day's stories, and pull full article text."""
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import trafilatura

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
WORD_RE = re.compile(r"[a-z0-9']+")
# Article pages are fetched with ordinary browser headers. Several outlets (AP,
# Politico, KERA, the Marshall Project) answer 403 to anything that looks like a bot.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# Hard paywalls: Google News local results from these never have readable text.
PAYWALLED = {"The New York Times", "The Washington Post", "The Wall Street Journal",
             "The Dallas Morning News", "Bloomberg", "Bloomberg.com", "Financial Times"}
SKIP_LINK_RE = re.compile(r"/(video|videos|podcast|podcasts|live)/|sponsored", re.I)
MIN_TEXT = 400  # characters; anything shorter is a teaser or a failed extraction
PARA_SPLIT_RE = re.compile(r"</p\s*>|<br\s*/?>\s*<br\s*/?>|</h[1-6]>|</li>", re.I)
LDJSON_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)
P_RE = re.compile(r"<p[\s>].*?</p\s*>", re.I | re.S)
STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "at", "as", "by",
        "with", "from", "after", "over", "says", "say", "new", "his", "her", "its", "be", "was"}


def clean_text(raw):
    text = html.unescape(TAG_RE.sub(" ", raw or ""))
    return WS_RE.sub(" ", text).strip()


def html_paragraphs(raw):
    """Split an HTML fragment into clean paragraphs."""
    paras = [clean_text(part) for part in PARA_SPLIT_RE.split(raw or "")]
    return [p for p in paras if len(p) > 1]


def _feed_body(entry):
    """Some feeds (Axios, WordPress sites) carry the whole article. Use it when they do."""
    for content in entry.get("content") or []:
        paras = html_paragraphs(content.get("value", ""))
        if sum(len(p) for p in paras) >= MIN_TEXT:
            return paras
    return []


def shorten(text, words=55):
    parts = text.split()
    if len(parts) <= words:
        return text
    cut = " ".join(parts[:words])
    # Prefer ending on a sentence.
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if end > len(cut) * 0.5:
        return cut[: end + 1]
    return cut.rstrip(",;:") + "…"


def _published(entry):
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def _image(entry):
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = media.get("url")
            if url and (media.get("medium", "image") == "image" or "image" in media.get("type", "image")):
                return url
    for enc in entry.get("enclosures") or []:
        if "image" in (enc.get("type") or "") and enc.get("href"):
            return enc["href"]
    for content in entry.get("content") or []:
        m = IMG_RE.search(content.get("value", ""))
        if m:
            return m.group(1)
    m = IMG_RE.search(entry.get("summary", ""))
    return m.group(1) if m else None


def fetch_feed(feed, cfg, since):
    resp = requests.get(feed["url"], headers={"User-Agent": cfg["user_agent"]}, timeout=20)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    items = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title"))
        link = entry.get("link")
        if not title or not link:
            continue
        source = feed["name"]
        if feed["kind"] == "gnews":
            # Google News titles end with " - Outlet".
            outlet = (entry.get("source") or {}).get("title")
            if outlet and title.endswith(" - " + outlet):
                title = title[: -len(outlet) - 3]
            if feed["name"] == "Google News" and outlet:
                source = outlet
            if outlet in PAYWALLED:
                continue
        if SKIP_LINK_RE.search(link):
            continue  # videos, podcasts, live blogs and ads have no article text
        published = _published(entry)
        if published and published < since:
            continue
        summary = "" if feed.get("no_summary") else clean_text(entry.get("summary"))
        if feed["kind"] == "gnews":
            summary = ""  # Google News summaries just repeat the headline.
        match = feed.get("match")
        if match:
            hay = (title + " " + summary).lower()
            if not any(word in hay for word in match):
                continue
        body = [] if feed["kind"] == "gnews" else _feed_body(entry)
        if body and not summary:
            summary = " ".join(body[:3])
        items.append({
            "section": feed["section"], "source": source, "title": title, "link": link, "paragraphs": body,
            "gnews": feed["kind"] == "gnews", "summary": shorten(summary),
            "published": published.isoformat() if published else None,
            "image": None if feed["kind"] == "gnews" else _image(entry),
        })
    return items


def _key_words(title):
    return {w for w in WORD_RE.findall(title.lower()) if w not in STOP and len(w) > 2}


def _is_dup(title, seen):
    words = _key_words(title)
    if not words:
        return False
    for other in seen:
        inter = len(words & other)
        if inter >= 3 and inter / len(words | other) >= 0.5:
            return True
    return False


def gather(feeds, cfg, now):
    """Fetch all feeds in parallel. Returns (items, errors)."""
    since = now - timedelta(hours=36)
    items, errors = [], []
    with ThreadPoolExecutor(max_workers=cfg["fetch_workers"]) as pool:
        futures = {pool.submit(fetch_feed, f, cfg, since): f for f in feeds}
        for fut, feed in futures.items():
            try:
                items.extend(fut.result())
            except Exception as exc:
                errors.append(f"{feed['name']} ({feed['section']}): {exc}"[:240])
    return items, errors


def pick(items, section_order, limits):
    """Round-robin across outlets so no single source dominates a section,
    newest first within each outlet, and drop near-duplicate headlines across
    the whole paper."""
    seen_words, seen_links = [], set()
    chosen = {}
    for section in section_order:
        by_source = {}
        for item in items:
            if item["section"] == section:
                by_source.setdefault(item["source"], []).append(item)
        for lst in by_source.values():
            lst.sort(key=lambda i: i["published"] or "", reverse=True)
        queues = list(by_source.values())
        picked = []
        while queues and len(picked) < limits.get(section, 6):
            for queue in list(queues):
                if not queue:
                    queues.remove(queue)
                    continue
                item = queue.pop(0)
                if item["link"] in seen_links or _is_dup(item["title"], seen_words):
                    continue
                seen_links.add(item["link"])
                seen_words.append(_key_words(item["title"]))
                picked.append(item)
                if len(picked) >= limits.get(section, 6):
                    break
        chosen[section] = picked
    return chosen


def prefer_readable(chosen, limits):
    """Trim each section back to its limit, letting spare stories whose full text
    came through stand in for ones that only have a summary. Order is kept."""
    out = {}
    for section, stories in chosen.items():
        limit = limits.get(section, 6)
        readable = [s for s in stories if s.get("paragraphs")]
        keep_ids = {id(s) for s in readable[:limit]}
        for s in stories:
            if len(keep_ids) >= limit:
                break
            keep_ids.add(id(s))
        out[section] = [s for s in stories if id(s) in keep_ids]
    return out


def _resolve_gnews(urls):
    """Turn news.google.com links into the publisher's own links."""
    if not urls:
        return {}
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        return {}
    out = {}
    for i in range(0, len(urls), 10):
        batch = urls[i:i + 10]
        try:
            results = gnewsdecoder(batch, timeout=15)
        except Exception:
            continue
        for url, res in zip(batch, results):
            if res.get("success") and res.get("decoded_url"):
                out[url] = res["decoded_url"]
        time.sleep(1)
    return out


def extract_paragraphs(doc, url):
    """Strict extraction first; if that throws the article away (PBS pages do),
    retry in the default and then the lenient mode."""
    for mode in ({"favor_precision": True}, {}, {"favor_recall": True}):
        text = trafilatura.extract(doc, include_comments=False, include_tables=False, url=url, **mode)
        if text and len(text) >= MIN_TEXT:
            return [p.strip() for p in text.split("\n") if len(p.strip()) > 1]
    # Many news sites put the whole story in their structured data.
    for block in LDJSON_RE.findall(doc):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                body = node.get("articleBody")
                if isinstance(body, str) and len(body) >= MIN_TEXT:
                    paras = html_paragraphs(body) if "<" in body else [
                        p.strip() for p in re.split(r"\n+", body) if p.strip()]
                    return paras
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    # Last resort: the page's own paragraphs, keeping only sentence-length ones.
    paras = [clean_text(p) for p in P_RE.findall(doc)]
    paras = [p for p in paras if len(p) >= 80]
    if sum(len(p) for p in paras) >= MIN_TEXT * 2:
        return paras
    return []


def _article(item, cfg):
    resp = requests.get(item["link"], headers=BROWSER_HEADERS, timeout=20)
    resp.raise_for_status()
    if resp.status_code == 202:  # AWS WAF answers bots with a script-only challenge page
        raise RuntimeError(f"bot check page (HTTP 202) for url: {item['link']}")
    doc = resp.text
    paras = extract_paragraphs(doc, item["link"])
    meta = trafilatura.extract_metadata(doc, default_url=item["link"])
    return {
        "paragraphs": paras,
        "author": getattr(meta, "author", None) if meta else None,
        "image": getattr(meta, "image", None) if meta else None,
    }


def add_full_text(chosen, cfg):
    stories = [s for lst in chosen.values() for s in lst]
    mapping = _resolve_gnews([s["link"] for s in stories if s["gnews"]])
    for s in stories:
        if s["link"] in mapping:
            s["link"] = mapping[s["link"]]
    if not cfg["full_text"]:
        return []
    errors = []

    def work(story):
        if story.get("paragraphs"):
            return  # the feed already carried the whole article
        if "news.google.com" in story["link"]:
            return
        if SKIP_LINK_RE.search(story["link"]):
            return
        try:
            art = _article(story, cfg)
        except Exception as exc:
            errors.append(f"text {story['source']}: {exc}"[:200])
            return
        if not art["paragraphs"]:
            errors.append(f"text {story['source']}: no article text found at {story['link']}"[:200])
        story["paragraphs"] = art["paragraphs"]
        story["author"] = art["author"]
        if not story.get("image") and art["image"] and art["image"].startswith("http"):
            story["image"] = art["image"]
        if not story["summary"] and art["paragraphs"]:
            story["summary"] = shorten(" ".join(art["paragraphs"][:3]))

    with ThreadPoolExecutor(max_workers=cfg["fetch_workers"]) as pool:
        list(pool.map(work, stories))
    return errors
