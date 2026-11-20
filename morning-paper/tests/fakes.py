"""Offline stand-ins for the network, so the whole build can be tested
without reaching any news site. Headlines here are made-up samples."""
import json
from urllib.parse import parse_qs, urlparse

SAMPLE = {
    "politics": [
        "Senate passes stopgap spending bill hours before shutdown deadline",
        "House committee schedules hearing on election security funding",
        "Governors push Congress for disaster relief after winter storms",
        "Supreme Court agrees to hear case on federal agency power",
        "Voting rights groups sue over new mail ballot rules",
        "White House outlines plan to lower prescription drug prices",
    ],
    "national": [
        "Winter storm brings heavy snow to the Great Lakes ahead of the holiday",
        "Airlines brace for record Thanksgiving travel week",
        "Federal Reserve minutes show officials split on next rate move",
        "Wildfire crews gain ground as winds ease in Southern California",
        "New report finds teacher shortages easing in most states",
        "NASA sets new launch window for lunar cargo mission",
    ],
    "tech": [
        "Chipmakers race to build new factories as demand for AI hardware grows",
        "Open-source project ships a faster, smaller web browser engine",
        "Regulators open inquiry into app store payment rules",
        "Why your next laptop battery could last two days",
        "Researchers show a cheap fix for a common Wi-Fi router flaw",
    ],
    "investigations": [
        "How a little-known rule kept thousands of families from getting housing aid",
        "Inside the private prisons that billed states for empty beds",
    ],
    "local": [
        "Dallas council approves plan to expand DART service to the airport",
        "Trinity River park project breaks ground after years of delays",
        "North Texas schools add counselors with new state grant",
        "Deep Ellum music venue reopens after renovation",
    ],
}

PARAS = [
    "The measure cleared its final hurdle late Thursday after a day of negotiations that stretched past midnight, according to officials familiar with the talks.",
    "Supporters said the change would take effect early next year, while critics warned that the timeline left little room for public comment.",
    "“We have been working on this for a long time, and today we finally have something to show for it,” one official said at a news conference.",
    "Analysts expect the decision to shape similar debates in other states over the coming months.",
    "The next step is a review period that is expected to last about six weeks.",
]


class Resp:
    def __init__(self, body, status=200, ctype="application/xml"):
        self.content = body.encode() if isinstance(body, str) else body
        self.text = self.content.decode("utf-8", "replace")
        self.status_code = status
        self.ok = status < 400
        self.headers = {"Content-Type": ctype}

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text)


def rss(section, source, n_start, image_base):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    items = []
    for i, title in enumerate(SAMPLE[section]):
        pub = (now - timedelta(hours=i * 2 + n_start)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        img = f'<media:content url="{image_base}/{section}-{i % 4}.jpg" medium="image"/>' if i % 2 == 0 else ""
        items.append(
            f"<item><title>{title}</title><link>https://example.org/{section}/{source}/{i}</link>"
            f"<pubDate>{pub}</pubDate><description>{PARAS[i % len(PARAS)]} {PARAS[(i + 1) % len(PARAS)]}</description>{img}</item>")
    return ('<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>'
            f"<title>{source}</title>{''.join(items)}</channel></rss>")


def make_get(image_base="http://127.0.0.1:8799/img", fail=()):
    """A fake requests.get. `fail` lists URL substrings that should error."""
    from paper.sources import FEEDS, DALLAS_LOCAL
    lookup = {f["url"]: f for f in FEEDS + DALLAS_LOCAL}

    def get(url, params=None, headers=None, timeout=None, **kw):
        for bad in fail:
            if bad in url:
                return Resp("down", 503)
        if url in lookup:
            f = lookup[url]
            return Resp(rss(f["section"], f["name"].replace(" ", ""), len(f["name"]) % 3, image_base))
        host = urlparse(url).netloc
        if host == "api.open-meteo.com":
            days = ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"]
            return Resp(json.dumps({
                "current": {"temperature_2m": 51.2, "apparent_temperature": 48.0, "weather_code": 2,
                            "wind_speed_10m": 9.4, "relative_humidity_2m": 62},
                "daily": {"time": days, "weather_code": [2, 61, 3, 0],
                          "temperature_2m_max": [64.1, 58.0, 55.2, 61.7],
                          "temperature_2m_min": [44.3, 47.9, 41.0, 39.5],
                          "precipitation_probability_max": [10, 70, 20, 0],
                          "sunrise": [d + "T06:58" for d in days], "sunset": [d + "T17:23" for d in days]},
            }))
        if host == "api.weather.gov":
            if "/points/" in url:
                return Resp(json.dumps({"properties": {"forecast": "https://api.weather.gov/gridpoints/FWD/89,104/forecast"}}))
            if "/forecast" in url:
                return Resp(json.dumps({"properties": {"periods": [{"name": "Today", "detailedForecast":
                    "Partly sunny, with a high near 64. North wind 5 to 10 mph."}]}}))
            if "/alerts" in url:
                return Resp(json.dumps({"features": []}))
        if host == "nominatim.openstreetmap.org":
            lat = float((params or {}).get("lat", 0))
            city = ("Dallas", "Texas") if lat < 35 else ("Chicago", "Illinois")
            return Resp(json.dumps({"address": {"city": city[0], "state": city[1], "country_code": "us"}}))
        if host == "api.bigdatacloud.net":
            return Resp(json.dumps({"city": "Austin", "principalSubdivision": "Texas", "countryCode": "US"}))
        if host == "example.org":
            body = "<html><head><title>x</title></head><body><article>" + "".join(
                f"<p>{p}</p>" for p in PARAS) + "</article></body></html>"
            return Resp(body, ctype="text/html")
        return Resp("not found", 404)

    return get


def install(monkey_modules, get):
    for mod in monkey_modules:
        mod.requests.get = get
