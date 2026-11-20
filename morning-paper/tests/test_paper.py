"""Run from morning-paper/:  python -m unittest discover tests"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

TMP = Path(tempfile.mkdtemp(prefix="paper-test-"))
os.environ["PAPER_DATA"] = str(TMP / "data")
os.environ["PAPER_CONFIG"] = str(TMP / "config.json")
(TMP / "config.json").write_text(json.dumps({"token": "secret-token", "port": 18790}))

from paper import build, location, news, scheduler, server, settings, weather  # noqa: E402
from tests import fakes  # noqa: E402

MODULES = (news, weather, location)


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(settings.DATA_DIR, ignore_errors=True)
        self.cfg = settings.load()
        self._orig = [m.requests.get for m in MODULES]
        fakes.install(MODULES, fakes.make_get())
        # Google News link decoding needs the network; skip it in tests.
        self._dec = mock.patch.object(news, "_resolve_gnews", lambda urls: {})
        self._dec.start()

    def tearDown(self):
        for m, g in zip(MODULES, self._orig):
            m.requests.get = g
        self._dec.stop()


class TestPieces(Base):
    def test_parse_coords(self):
        self.assertEqual(location.parse_coords({"lat": "32.7", "lon": "-96.8"}), (32.7, -96.8))
        self.assertEqual(location.parse_coords({"location": "41.88, -87.63"}), (41.88, -87.63))
        with self.assertRaises(ValueError):
            location.parse_coords({"lat": 200, "lon": 0})

    def test_location_near_home_and_away(self):
        place, near = location.current(self.cfg)
        self.assertTrue(near)
        location.save(41.88, -87.63, self.cfg)
        place, near = location.current(self.cfg)
        self.assertFalse(near)
        self.assertEqual(location.label(place), "Chicago, Illinois")

    def test_geocoder_fallback(self):
        fakes.install(MODULES, fakes.make_get(fail=("nominatim",)))
        place = location.save(30.27, -97.74, self.cfg)
        self.assertEqual(location.label(place), "Austin, Texas")

    def test_pick_round_robin_and_dedupe(self):
        words = ["harbor", "budget", "comet", "orchard", "tunnel", "glacier", "violin", "harvest", "museum", "ferry"]
        items = []
        for src in ("A", "B"):
            for i in range(5):
                w = words[i if src == "A" else i + 5]
                items.append({"section": "national", "source": src, "title": f"{w} {words[(i * 3 + 1) % 10]}s {src.lower()}xq{i} update",
                              "link": f"https://x/{src}{i}", "published": f"2026-11-20T0{i}:00:00+00:00"})
        items.append({"section": "national", "source": "C", "title": "tunnel harbors axq4 update",
                      "link": "https://x/dup", "published": "2026-11-20T09:00:00+00:00"})
        chosen = news.pick(items, ["national"], {"national": 4})["national"]
        self.assertEqual(len(chosen), 4)
        self.assertEqual({s["source"] for s in chosen[:2]}, {"A", "B"})
        self.assertNotIn("https://x/dup", [s["link"] for s in chosen])

    def test_full_text_uses_browser_headers_and_fallback(self):
        seen = {}
        page = "<html><body><article>" + "".join(f"<p>{p}</p>" for p in fakes.PARAS) + "</article></body></html>"

        def get(url, headers=None, **kw):
            seen["ua"] = (headers or {}).get("User-Agent", "")
            return fakes.Resp(page, ctype="text/html")

        calls = []
        real = news.trafilatura.extract

        def picky(doc, **kw):  # the strict mode throws the article away, as on PBS
            calls.append(kw)
            return None if kw.get("favor_precision") else real(doc, **kw)

        with mock.patch.object(news.requests, "get", get), mock.patch.object(news.trafilatura, "extract", picky):
            art = news._article({"link": "https://www.pbs.org/newshour/x"}, self.cfg)
        self.assertIn("Chrome", seen["ua"])
        self.assertGreaterEqual(len(art["paragraphs"]), 3)
        self.assertTrue(calls[0].get("favor_precision"))
        self.assertGreater(len(calls), 1)

    def test_skips_paywalls_and_videos(self):
        from datetime import timezone
        rss = ('<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>'
               '<item><title>Story one - The New York Times</title><link>https://news.google.com/a</link>'
               '<source url="https://nytimes.com">The New York Times</source></item>'
               '<item><title>Story two - KERA News</title><link>https://news.google.com/b</link>'
               '<source url="https://kera.org">KERA News</source></item>'
               "</channel></rss>")
        clip = ('<?xml version="1.0"?><rss version="2.0"><channel><title>p</title>'
                '<item><title>A clip</title><link>https://www.politico.com/video/2026/11/20/x</link></item>'
                "</channel></rss>")
        with mock.patch.object(news.requests, "get", lambda *a, **k: fakes.Resp(rss)):
            gn = news.fetch_feed({"name": "Google News", "kind": "gnews", "section": "local", "url": "u"},
                                 self.cfg, datetime(2000, 1, 1, tzinfo=timezone.utc))
        with mock.patch.object(news.requests, "get", lambda *a, **k: fakes.Resp(clip)):
            pol = news.fetch_feed({"name": "Politico", "kind": "rss", "section": "politics", "url": "u"},
                                  self.cfg, datetime(2000, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([i["source"] for i in gn], ["KERA News"])
        self.assertEqual(pol, [])

    def test_structured_data_and_feed_body_fallbacks(self):
        body = " ".join(fakes.PARAS)
        page = ('<html><head><script type="application/ld+json">'
                + json.dumps({"@graph": [{"@type": "NewsArticle", "articleBody": body}]})
                + "</script></head><body><div id=app></div></body></html>")
        with mock.patch.object(news.trafilatura, "extract", lambda doc, **kw: None):
            self.assertEqual(news.extract_paragraphs(page, "https://www.pbs.org/x"), [body])
        entry = {"content": [{"value": "".join(f"<p>{p}</p>" for p in fakes.PARAS)}]}
        self.assertEqual(news._feed_body(entry), fakes.PARAS)
        self.assertEqual(news._feed_body({"content": [{"value": "<p>Teaser only.</p>"}]}), [])

    def test_prefer_readable_swaps_in_spares(self):
        mk = lambda n, ok: {"title": n, "paragraphs": ["x"] if ok else []}
        stories = [mk("a", True), mk("b", False), mk("c", True), mk("d", True), mk("e", False)]
        out = news.prefer_readable({"politics": stories}, {"politics": 3})
        self.assertEqual([s["title"] for s in out["politics"]], ["a", "c", "d"])
        short = [mk("a", False), mk("b", True)]
        out = news.prefer_readable({"politics": short}, {"politics": 2})
        self.assertEqual([s["title"] for s in out["politics"]], ["a", "b"])

    def test_shorten(self):
        text = "One two three. " * 30
        self.assertLess(len(news.shorten(text, 20).split()), 21)

    def test_expected_date(self):
        tz = self.cfg["tz"]
        before = datetime(2026, 11, 20, 5, 0, tzinfo=tz)
        after = datetime(2026, 11, 20, 7, 30, tzinfo=tz)
        self.assertEqual(scheduler.expected_date(self.cfg, before).isoformat(), "2026-11-19")
        self.assertEqual(scheduler.expected_date(self.cfg, after).isoformat(), "2026-11-20")


class TestBuild(Base):
    def test_full_build(self):
        result = build.build(self.cfg)
        self.assertEqual(result["status"], "built", result)
        day = result["date"]
        out = settings.DATA_DIR / "issues" / day
        html = (out / "index.html").read_text()
        self.assertIn(self.cfg["paper_name"], html)
        self.assertIn("National Politics", html)
        self.assertTrue((out / "paper.epub").stat().st_size > 1000)
        self.assertTrue((out / "story-1.html").exists())
        issue = json.loads((out / "issue.json").read_text())
        counts = {s["key"]: len(s["stories"]) for s in issue["sections"]}
        self.assertEqual(counts["politics"], 6)  # fewer unique samples than the limit
        self.assertGreater(counts["local"], 0)
        self.assertEqual(build.build(self.cfg)["status"], "exists")

    def test_build_survives_feed_outages(self):
        fakes.install(MODULES, fakes.make_get(fail=("npr.org", "open-meteo")))
        result = build.build(self.cfg)
        self.assertEqual(result["status"], "built")
        self.assertTrue(any("NPR" in e for e in result["feed_errors"]))

    def test_away_adds_second_weather_box(self):
        location.save(41.88, -87.63, self.cfg)
        build.build(self.cfg)
        issue = json.loads(next((settings.DATA_DIR / "issues").glob("*/issue.json")).read_text())
        self.assertEqual([b["name"] for b in issue["weather"]], ["Dallas", "Chicago, Illinois"])


class TestServer(Base):
    @classmethod
    def setUpClass(cls):
        cfg = settings.load()
        server.Handler.cfg = cfg
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, path, data=None, headers=None):
        r = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_routes(self):
        self.assertEqual(self.req("/")[0], 200)  # waiting page
        self.assertEqual(self.req("/health")[0], 503)
        build.build(self.cfg)
        code, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"National Politics", body)
        self.assertEqual(self.req("/static/paper.css")[0], 200)
        self.assertEqual(self.req("/static/../settings.py")[0], 404)
        self.assertEqual(self.req("/today.epub")[0], 200)
        self.assertEqual(self.req("/archive")[0], 200)

    def test_location_needs_token(self):
        body = json.dumps({"lat": 41.88, "lon": -87.63}).encode()
        self.assertEqual(self.req("/api/location", body, {"Content-Type": "application/json"})[0], 401)
        code, resp = self.req("/api/location", body, {"Content-Type": "application/json",
                                                      "Authorization": "Bearer secret-token"})
        self.assertEqual(code, 200, resp)
        self.assertEqual(json.loads(resp)["saved"], "Chicago, Illinois")
        code, _ = self.req("/api/location?token=secret-token", b"lat=32.7&lon=-96.8",
                           {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(code, 200)


if __name__ == "__main__":
    unittest.main()
