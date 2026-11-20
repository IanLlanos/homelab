"""Your last known location, posted by the iPhone Shortcut."""
import json
import math
from datetime import datetime, timezone

import requests

from .settings import data_dir

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def _path():
    return data_dir() / "location.json"


def distance_km(a_lat, a_lon, b_lat, b_lon):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def reverse_geocode(lat, lon, cfg):
    """City and state for a point, via OpenStreetMap Nominatim."""
    resp = requests.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1},
        headers={"User-Agent": cfg["user_agent"], "Accept-Language": "en"},
        timeout=15,
    )
    resp.raise_for_status()
    addr = resp.json().get("address", {})
    city = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("county"))
    return {
        "city": city,
        "state": addr.get("state"),
        "country_code": (addr.get("country_code") or "").upper(),
    }


def parse_coords(payload):
    """Accept {"lat":..,"lon":..}, {"latitude":..,"longitude":..} or {"location":"lat,lon"}."""
    lat = payload.get("lat", payload.get("latitude"))
    lon = payload.get("lon", payload.get("lng", payload.get("longitude")))
    if (lat is None or lon is None) and payload.get("location"):
        parts = str(payload["location"]).replace(" ", "").split(",")
        if len(parts) >= 2:
            lat, lon = parts[0], parts[1]
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates out of range")
    return lat, lon


def save(lat, lon, cfg):
    place = {"lat": round(lat, 4), "lon": round(lon, 4),
             "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        place.update(reverse_geocode(lat, lon, cfg))
    except Exception as exc:  # keep the coordinates even if the geocoder is down
        place["geocode_error"] = str(exc)[:200]
        old = load_raw()
        if old and distance_km(lat, lon, old["lat"], old["lon"]) < 25:
            for key in ("city", "state", "country_code"):
                place.setdefault(key, old.get(key))
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(place, indent=2), encoding="utf-8")
    tmp.replace(_path())
    return place


def load_raw():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def current(cfg):
    """(place, near_home). Falls back to home when nothing has been posted."""
    home = cfg["home"]
    place = load_raw()
    if not place:
        return ({"lat": home["lat"], "lon": home["lon"], "city": home["name"],
                 "state": home.get("state"), "country_code": "US", "updated": None}, True)
    near = distance_km(place["lat"], place["lon"], home["lat"], home["lon"]) <= cfg["home_radius_km"]
    return place, near


def label(place):
    city, state = place.get("city"), place.get("state")
    if city and state:
        return f"{city}, {state}"
    return city or state or f"{place['lat']:.2f}, {place['lon']:.2f}"
