"""Small web server: the paper, story pages, EPUB, archive, health, and the
location endpoint for the iPhone Shortcut."""
import hmac
import json
import logging
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import build, location, render, scheduler
from .settings import PACKAGE_DIR, data_dir

log = logging.getLogger("paper.server")

ISSUE_RE = re.compile(r"^/issue/(\d{4}-\d{2}-\d{2})/(index\.html|story-\d{1,3}\.html|paper\.epub)?$")
STATIC_RE = re.compile(r"^/static/([a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?)$")
MAX_BODY = 4096
TOKEN_RE = re.compile(r"([?&])token=[^&\s]*")


class Handler(BaseHTTPRequestHandler):
    cfg = None
    server_version = "morning-paper"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), TOKEN_RE.sub(r"\1token=***", fmt % args))

    # ---- helpers ----
    def _send(self, code, body, ctype="text/html; charset=utf-8", cache="no-cache", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _file(self, path, cache="no-cache"):
        if not path.is_file():
            return self._send(404, "Not found", "text/plain")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        extra = {}
        if path.suffix == ".epub":
            extra["Content-Disposition"] = f'attachment; filename="{path.parent.name}-paper.epub"'
        self._send(200, path.read_bytes(), ctype, cache, extra)

    def _authorized(self, query):
        token = self.cfg.get("token") or ""
        if not token:
            return False
        given = ""
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        given = given or self.headers.get("X-Paper-Token", "") or query.get("token", [""])[0]
        return hmac.compare_digest(given.encode(), token.encode())

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype or raw.strip().startswith(b"{"):
            return json.loads(raw or b"{}")
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}

    # ---- routes ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        issues = build.issues_dir()
        if path in ("/", "/index.html"):
            dates = build.issue_dates()
            if not dates:
                env = render.make_env(self.cfg)
                return self._send(200, env.get_template("waiting.html.j2").render())
            return self._file(issues / dates[0].isoformat() / "index.html")
        if path in ("/today.epub", "/latest.epub"):
            dates = build.issue_dates()
            if not dates:
                return self._send(404, "No issue yet", "text/plain")
            return self._file(issues / dates[0].isoformat() / "paper.epub")
        if path == "/archive":
            return self._file(data_dir() / "archive.html")
        if path == "/health":
            return self._health()
        m = ISSUE_RE.match(path)
        if m:
            day, name = m.group(1), m.group(2) or "index.html"
            return self._file(issues / day / name, cache="max-age=300")
        m = STATIC_RE.match(path)
        if m:
            target = (PACKAGE_DIR / "static" / m.group(1)).resolve()
            if (PACKAGE_DIR / "static") in target.parents:
                return self._file(target, cache="max-age=604800")
        if path == "/favicon.ico":
            return self._file(PACKAGE_DIR / "static" / "icon.png", cache="max-age=604800")
        self._send(404, "Not found", "text/plain")

    def _health(self):
        dates = build.issue_dates()
        expected = scheduler.expected_date(self.cfg)
        latest = dates[0] if dates else None
        ok = latest is not None and latest >= expected
        self._json(200 if ok else 503, {
            "status": "ok" if ok else "stale",
            "latest_issue": latest.isoformat() if latest else None,
            "expected_issue": expected.isoformat(),
        })

    def do_POST(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path not in ("/api/location", "/api/build"):
            return self._send(404, "Not found", "text/plain")
        if not self._authorized(query):
            return self._json(401, {"error": "bad or missing token"})
        if url.path == "/api/location":
            try:
                lat, lon = location.parse_coords(self._body())
            except (ValueError, TypeError, KeyError) as exc:
                return self._json(400, {"error": f"need lat and lon: {exc}"})
            place = location.save(lat, lon, self.cfg)
            return self._json(200, {"saved": location.label(place)})
        # /api/build: rebuild today's issue in the background.
        force = query.get("force", ["1"])[0] != "0"
        threading.Thread(target=self._rebuild, args=(force,), daemon=True).start()
        return self._json(202, {"status": "building"})

    def _rebuild(self, force):
        try:
            build.build(self.cfg, force=force)
        except Exception:
            log.exception("Manual build failed")


def serve(cfg):
    Handler.cfg = cfg
    mimetypes.add_type("application/epub+zip", ".epub")
    mimetypes.add_type("font/woff2", ".woff2")
    httpd = ThreadingHTTPServer(("0.0.0.0", int(cfg["port"])), Handler)
    httpd.daemon_threads = True
    log.info("Serving on port %s", cfg["port"])
    httpd.serve_forever()

