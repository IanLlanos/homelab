"""Turn an issue into HTML pages and an EPUB."""
import html
from datetime import date, datetime

from ebooklib import epub
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .settings import PACKAGE_DIR, data_dir

STATIC_VERSION = "6"


def _dt(value, tz):
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(tz)


SKY_SHORT = {"Partly cloudy": "Pt. cloudy", "Mostly clear": "Mostly clr", "Thunderstorms": "T-storms",
             "Heavy showers": "Hvy showers", "Freezing drizzle": "Frz. drizzle", "Freezing rain": "Frz. rain"}


def hourly_chart(hours):
    """A compact, script-free SVG of the day: temperature and feels-like lines on
    one scale, chance of rain as bars in their own strip below, sky every 3 hours.
    Colors come from the page's CSS, so day/night/e-ink all apply."""
    n = len(hours or [])
    if n < 2:
        return ""
    W, L, R = 480, 30, 44
    T0, T1 = 16, 88           # temperature panel
    B0, B1 = 104, 132         # rain strip (baseline at B1)
    span = (W - L - R) / (n - 1)
    x = [L + i * span for i in range(n)]
    temps = [h["temp"] for h in hours]
    feels = [h["feels"] for h in hours]
    lo = (min(temps + feels) - 1) // 10 * 10
    hi = -(-(max(temps + feels) + 1) // 10) * 10
    if hi - lo < 10:
        hi = lo + 10
    ty = lambda v: T1 - (v - lo) / (hi - lo) * (T1 - T0)
    out = [f'<svg class="hc" viewBox="0 0 {W} 176" role="img" aria-label="Hourly temperature, '
           f'feels-like and chance of rain, {html.escape(hours[0]["hour"])} to {html.escape(hours[-1]["hour"])}">']
    for v in range(lo, hi + 1, 10):
        y = ty(v)
        out.append(f'<line class="hc-grid" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="hc-ax" x="{L - 5}" y="{y + 3.5:.1f}" text-anchor="end">{v}°</text>')
    bw = max(2.0, min(14.0, span - 2))
    for i, h in enumerate(hours):
        bh = max(0.0, min(100, h["rain"])) / 100 * (B1 - B0)
        if bh:
            cls = "hc-bar wet" if h["rain"] >= 50 else "hc-bar"
            out.append(f'<rect class="{cls}" x="{x[i] - bw / 2:.1f}" y="{B1 - bh:.1f}" width="{bw:.1f}" '
                       f'height="{bh:.1f}" rx="1.5"/>')
    out.append(f'<line class="hc-base" x1="{L}" x2="{W - R}" y1="{B1}" y2="{B1}"/>'
               f'<text class="hc-ax" x="{L - 5}" y="{B1 - 2}" text-anchor="end">Rain</text>')
    wettest = max(range(n), key=lambda i: hours[i]["rain"])
    if hours[wettest]["rain"] >= 20:
        bh = hours[wettest]["rain"] / 100 * (B1 - B0)
        out.append(f'<text class="hc-val" x="{x[wettest]:.1f}" y="{B1 - bh - 3:.1f}" '
                   f'text-anchor="middle">{hours[wettest]["rain"]}%</text>')
    pts = lambda vals: " ".join(f"{x[i]:.1f},{ty(v):.1f}" for i, v in enumerate(vals))
    out.append(f'<polyline class="hc-feels" points="{pts(feels)}"/>'
               f'<polyline class="hc-temp" points="{pts(temps)}"/>')
    # Direct labels at the right end, nudged apart if they would touch.
    yt, yf = ty(temps[-1]), ty(feels[-1])
    if abs(yt - yf) < 11:
        mid = (yt + yf) / 2
        yt, yf = (mid - 5.5, mid + 5.5) if temps[-1] >= feels[-1] else (mid + 5.5, mid - 5.5)
    out.append(f'<text class="hc-lab" x="{W - R + 5}" y="{yt + 3.5:.1f}">Temp</text>'
               f'<text class="hc-lab muted" x="{W - R + 5}" y="{yf + 3.5:.1f}">Feels</text>')
    # The day's high and low on the temperature line.
    hi_i = max(range(n), key=lambda i: temps[i])
    lo_i = min(range(n), key=lambda i: temps[i])
    for i in dict.fromkeys((hi_i, lo_i)):
        y = ty(temps[i])
        anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
        dx = 4 if i == 0 else -4 if i == n - 1 else 0
        # Above the line, where the dashed feels-like line (usually lower) is not.
        out.append(f'<circle class="hc-dot" cx="{x[i]:.1f}" cy="{y:.1f}" r="3.5"/>'
                   f'<text class="hc-val" x="{x[i] + dx:.1f}" y="{y - 8:.1f}" '
                   f'text-anchor="{anchor}">{temps[i]}°</text>')
    for i, h in enumerate(hours):
        if h.get("h24", 0) % 3 == 0 or n <= 6:
            anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
            sky = SKY_SHORT.get(h["conditions"], h["conditions"])
            out.append(f'<line class="hc-tick" x1="{x[i]:.1f}" x2="{x[i]:.1f}" y1="{B1}" y2="{B1 + 4}"/>'
                       f'<text class="hc-ax" x="{x[i]:.1f}" y="{B1 + 16}" text-anchor="{anchor}">'
                       f'{html.escape(h["hour"])}</text>'
                       f'<text class="hc-sky" x="{x[i]:.1f}" y="{B1 + 30}" text-anchor="{anchor}">'
                       f'{html.escape(sky)}</text>')
    # Invisible hit areas, one per hour, with the full reading as a tooltip.
    for i, h in enumerate(hours):
        x0 = (x[i - 1] + x[i]) / 2 if i else L - span / 2
        x1 = (x[i] + x[i + 1]) / 2 if i < n - 1 else W - R + span / 2
        out.append(f'<rect class="hc-hit" x="{x0:.1f}" y="{T0 - 8}" width="{x1 - x0:.1f}" height="{B1 - T0 + 8}">'
                   f'<title>{html.escape(h["hour"])}: {html.escape(h["conditions"])}, {h["temp"]}° '
                   f'(feels {h["feels"]}°), {h["rain"]}% rain, wind {h["wind"]} mph</title></rect>')
    out.append("</svg>")
    return Markup("".join(out))


def hourly_summary(hours):
    if not hours:
        return ""
    winds = [h["wind"] for h in hours]
    wind = f"{min(winds)}–{max(winds)}" if min(winds) != max(winds) else f"{winds[0]}"
    parts = [f"Wind {wind} mph"]
    wet = max(hours, key=lambda h: h["rain"])
    parts.append(f"Rain most likely around {wet['hour']} ({wet['rain']}%)" if wet["rain"] >= 20
                 else "Little chance of rain")
    return " · ".join(parts)


def make_env(cfg):
    env = Environment(loader=FileSystemLoader(PACKAGE_DIR / "templates"),
                      autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)
    tz = cfg["tz"]

    def clock(value):
        d = _dt(value, tz) if isinstance(value, str) else value
        return d.strftime("%-I:%M %p") if d else ""

    def when(value):
        d = _dt(value, tz)
        if not d:
            return ""
        today = datetime.now(tz).date()
        if d.date() == today:
            return d.strftime("%-I:%M %p")
        return d.strftime("%a %-I:%M %p")

    def hm12(value):  # "06:58" -> "6:58 AM"
        try:
            return datetime.strptime(value, "%H:%M").strftime("%-I:%M %p")
        except (TypeError, ValueError):
            return value or ""

    def longdate(value):
        return date.fromisoformat(value).strftime("%A, %B %-d, %Y")

    def weekday(value):
        return date.fromisoformat(value).strftime("%a")

    def roman(n):
        vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
        out = ""
        for v, s in vals:
            while n >= v:
                out += s
                n -= v
        return out

    env.filters.update(clock=clock, when=when, hm12=hm12, longdate=longdate, weekday=weekday, roman=roman,
                       hourly_chart=hourly_chart, hourly_summary=hourly_summary)
    env.globals.update(cfg=cfg, static_version=STATIC_VERSION)
    return env


def _lead(issue):
    """The front-page lead: the first national story with a photo, else the first story."""
    for key in ("national", "politics"):
        for sec in issue["sections"]:
            if sec["key"] == key:
                for story in sec["stories"]:
                    if story.get("image"):
                        return story
    for sec in issue["sections"]:
        if sec["stories"]:
            return sec["stories"][0]
    return None


def write_issue(issue, cfg, out_dir):
    env = make_env(cfg)
    lead = _lead(issue)
    sections = []
    for sec in issue["sections"]:
        rest = [s for s in sec["stories"] if s is not lead]
        sections.append({**sec, "stories": rest})
    page = env.get_template("issue.html.j2").render(issue=issue, lead=lead, sections=sections)
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    story_tpl = env.get_template("story.html.j2")
    for sec in issue["sections"]:
        for story in sec["stories"]:
            (out_dir / f"story-{story['id']}.html").write_text(
                story_tpl.render(issue=issue, story=story, section=sec), encoding="utf-8")
    write_epub(issue, cfg, out_dir / "paper.epub")


def write_archive(cfg, dates):
    env = make_env(cfg)
    page = env.get_template("archive.html.j2").render(dates=[d.isoformat() for d in dates])
    (data_dir() / "archive.html").write_text(page, encoding="utf-8")


def write_epub(issue, cfg, path):
    book = epub.EpubBook()
    title = f"{cfg['paper_name']}, {date.fromisoformat(issue['date']).strftime('%B %-d, %Y')}"
    book.set_identifier(f"morning-paper-{issue['date']}")
    book.set_title(title)
    book.set_language("en")
    book.add_author(cfg["paper_name"])
    css = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=(
        "body{font-family:serif;line-height:1.45}h1{font-size:1.3em;margin:1.2em 0 .2em}"
        "h2{font-size:1.1em;margin:1.4em 0 .2em}.meta{font-size:.8em;font-variant:small-caps;color:#444}"
        "p{margin:0 0 .6em;text-indent:0}"))
    book.add_item(css)
    chapters = []

    weather_html = ["<h1>The Weather</h1>"]
    for box in issue["weather"]:
        if box.get("ok"):
            weather_html.append(
                f"<h2>{html.escape(box['name'])}</h2><p>{box.get('today', '')}. "
                f"High {box.get('high', '–')}°, low {box.get('low', '–')}°, "
                f"{box.get('rain', 0)}% chance of rain.</p>")
            if box.get("forecast"):
                weather_html.append(f"<p>{html.escape(box['forecast'])}</p>")
            if box.get("hours"):
                weather_html.append("<p class='meta'>Hour by hour</p>")
                weather_html.append("".join(
                    f"<p>{h['hour']}: {html.escape(h['conditions'])}, {h['temp']}° "
                    f"(feels {h['feels']}°), {h['rain']}% rain, wind {h['wind']} mph</p>"
                    for h in box["hours"]))
    ch = epub.EpubHtml(title="The Weather", file_name="weather.xhtml", lang="en")
    ch.content = "".join(weather_html)
    ch.add_item(css)
    chapters.append(ch)

    for sec in issue["sections"]:
        if not sec["stories"]:
            continue
        parts = [f"<h1>{html.escape(sec['title'])}</h1>"]
        for s in sec["stories"]:
            parts.append(f"<h2>{html.escape(s['title'])}</h2>")
            parts.append(f"<p class='meta'>{html.escape(s['source'])}</p>")
            paras = s.get("paragraphs") or ([s["summary"]] if s.get("summary") else [])
            parts.extend(f"<p>{html.escape(p)}</p>" for p in paras)
            parts.append(f"<p class='meta'><a href='{html.escape(s['link'])}'>Original story</a></p>")
        ch = epub.EpubHtml(title=sec["title"], file_name=f"{sec['key']}.xhtml", lang="en")
        ch.content = "".join(parts)
        ch.add_item(css)
        chapters.append(ch)

    for ch in chapters:
        book.add_item(ch)
    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters
    epub.write_epub(str(path), book)
