"""Where each section's news comes from.

kind "rss":   a normal RSS/Atom feed.
kind "gnews": a Google News search, used for outlets without a usable public
              feed (AP, Reuters, local outlets). Links are resolved to the
              original article when the full text is fetched.

No right-leaning or conservative outlets, by request.
"""
from urllib.parse import quote_plus

SECTIONS = [
    ("politics", "National Politics"),
    ("national", "The Nation"),
    ("local", "Local"),
    ("tech", "Technology"),
    ("investigations", "Long Reads"),
]


def gnews(query):
    return ("https://news.google.com/rss/search?q=" + quote_plus(query)
            + "&hl=en-US&gl=US&ceid=US:en")


FEEDS = [
    # National politics
    {"section": "politics", "name": "NPR", "kind": "rss", "url": "https://feeds.npr.org/1014/rss.xml"},
    {"section": "politics", "name": "PBS News", "kind": "rss", "url": "https://www.pbs.org/newshour/feeds/rss/politics"},
    {"section": "politics", "name": "AP", "kind": "gnews", "url": gnews("site:apnews.com politics when:1d")},
    {"section": "politics", "name": "ProPublica", "kind": "rss", "url": "https://www.propublica.org/feeds/propublica/main", "match": ["politic", "congress", "white house", "election", "trump", "senate", "supreme court"]},
    {"section": "politics", "name": "Politico", "kind": "rss", "url": "https://rss.politico.com/politics-news.xml"},
    {"section": "politics", "name": "The 19th", "kind": "rss", "url": "https://19thnews.org/feed/"},
    {"section": "politics", "name": "The Guardian", "kind": "rss", "url": "https://www.theguardian.com/us-news/us-politics/rss"},
    # National general news
    {"section": "national", "name": "AP", "kind": "gnews", "url": gnews("site:apnews.com when:1d -sports")},
    {"section": "national", "name": "NPR", "kind": "rss", "url": "https://feeds.npr.org/1001/rss.xml"},
    {"section": "national", "name": "PBS News", "kind": "rss", "url": "https://www.pbs.org/newshour/feeds/rss/headlines"},
    {"section": "national", "name": "The Guardian", "kind": "rss", "url": "https://www.theguardian.com/us-news/rss"},
    {"section": "national", "name": "Axios", "kind": "rss", "url": "https://api.axios.com/feed/"},
    {"section": "national", "name": "Reuters", "kind": "gnews", "url": gnews("site:reuters.com world OR us when:1d")},
    # Technology
    {"section": "tech", "name": "Ars Technica", "kind": "rss", "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"section": "tech", "name": "The Verge", "kind": "rss", "url": "https://www.theverge.com/rss/index.xml"},
    {"section": "tech", "name": "Wired", "kind": "rss", "url": "https://www.wired.com/feed/rss"},
    {"section": "tech", "name": "404 Media", "kind": "rss", "url": "https://www.404media.co/rss/"},
    {"section": "tech", "name": "Hacker News", "kind": "rss", "url": "https://hnrss.org/frontpage?points=150", "no_summary": True},
    # Investigations / long reads
    {"section": "investigations", "name": "ProPublica", "kind": "rss", "url": "https://www.propublica.org/feeds/propublica/main"},
    {"section": "investigations", "name": "The Marshall Project", "kind": "rss", "url": "https://www.themarshallproject.org/rss/recent.rss"},
]

# Used when you're within home_radius_km of Dallas.
DALLAS_LOCAL = [
    {"section": "local", "name": "KERA News", "kind": "gnews", "url": gnews("site:keranews.org when:2d")},
    {"section": "local", "name": "The Texas Tribune", "kind": "gnews", "url": gnews("site:texastribune.org when:2d")},
    {"section": "local", "name": "Dallas Observer", "kind": "gnews", "url": gnews("site:dallasobserver.com when:2d")},
    {"section": "local", "name": "Google News", "kind": "gnews", "url": gnews('"Dallas" when:1d')},
]


def local_feeds(place, near_home):
    """Local section feeds for where you are right now."""
    if near_home:
        return [dict(f) for f in DALLAS_LOCAL]
    city, state = place.get("city"), place.get("state")
    feeds = []
    if city:
        q = f'"{city}"' + (f" {state}" if state else "")
        feeds.append({"section": "local", "name": "Google News", "kind": "gnews", "url": gnews(q + " when:1d")})
        feeds.append({"section": "local", "name": "Public radio", "kind": "gnews",
                      "url": gnews(f'"{city}" (NPR OR "public radio") when:3d')})
    elif state:
        feeds.append({"section": "local", "name": "Google News", "kind": "gnews", "url": gnews(f'"{state}" when:1d')})
    if state == "Texas":
        feeds.append({"section": "local", "name": "The Texas Tribune", "kind": "gnews", "url": gnews("site:texastribune.org when:2d")})
    return feeds


def all_feeds(cfg, place, near_home):
    disabled = {n.lower() for n in cfg.get("disabled_feeds", [])}
    feeds = [dict(f) for f in FEEDS] + local_feeds(place, near_home) + list(cfg.get("extra_feeds", []))
    return [f for f in feeds if f["name"].lower() not in disabled]
