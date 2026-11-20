"""python -m paper [serve | build [--force] [--date YYYY-MM-DD] | location LAT LON | health]"""
import argparse
import json
import logging
import sys
from datetime import date

from . import build, location, scheduler, server, settings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="paper", description="Personal morning newspaper")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="web server + daily schedule (default)")
    b = sub.add_parser("build", help="build an issue now")
    b.add_argument("--force", action="store_true", help="rebuild even if the issue exists")
    b.add_argument("--date", help="issue date, default today")
    loc = sub.add_parser("location", help="set the current location by hand")
    loc.add_argument("lat", type=float)
    loc.add_argument("lon", type=float)
    sub.add_parser("health", help="exit 0 when the expected issue exists")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = settings.load()
    cmd = args.cmd or "serve"

    if cmd == "serve":
        scheduler.start(cfg)
        server.serve(cfg)
    elif cmd == "build":
        day = date.fromisoformat(args.date) if args.date else None
        result = build.build(cfg, day, force=args.force)
        print(json.dumps(result, indent=2))
    elif cmd == "location":
        place = location.save(args.lat, args.lon, cfg)
        print(json.dumps(place, indent=2))
    elif cmd == "health":
        dates = build.issue_dates()
        ok = bool(dates) and dates[0] >= scheduler.expected_date(cfg)
        print("ok" if ok else "stale")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
