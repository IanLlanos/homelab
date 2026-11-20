"""Turn an issue into HTML pages and an EPUB."""
import html
from datetime import date, datetime

from ebooklib import epub
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .settings import PACKAGE_DIR, data_dir

STATIC_VERSION = "5"


def _dt(value, tz):
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(tz)


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

    env.filters.update(clock=clock, when=when, hm12=hm12, longdate=longdate, weekday=weekday, roman=roman)
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
