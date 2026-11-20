"""Weather boxes: Open-Meteo for the numbers, National Weather Service for
forecast wording and alerts when the place is in the US."""
import requests

WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow",
    73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Showers", 81: "Showers",
    82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorms", 96: "Thunderstorms, hail", 99: "Severe storms, hail",
}


def _open_meteo(lat, lon, ua):
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
            "hourly": "temperature_2m,apparent_temperature,precipitation_probability,weather_code,wind_speed_10m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "timezone": "auto", "forecast_days": 4,
        },
        headers={"User-Agent": ua}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _nws(lat, lon, ua):
    """Today's forecast sentence and active alerts. US only."""
    headers = {"User-Agent": ua, "Accept": "application/geo+json"}
    out = {"text": None, "period": None, "alerts": []}
    pts = requests.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=headers, timeout=15)
    pts.raise_for_status()
    fc = requests.get(pts.json()["properties"]["forecast"], headers=headers, timeout=15)
    fc.raise_for_status()
    periods = fc.json()["properties"]["periods"]
    if periods:
        out["text"] = periods[0]["detailedForecast"]
        out["period"] = periods[0]["name"]
    al = requests.get("https://api.weather.gov/alerts/active", params={"point": f"{lat:.4f},{lon:.4f}"},
                      headers=headers, timeout=15)
    if al.ok:
        for feat in al.json().get("features", [])[:3]:
            p = feat["properties"]
            out["alerts"].append({"event": p.get("event"), "headline": p.get("headline")})
    return out


def _hm(hour):
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def hourly(om, first_hour=6):
    """Today's hours from 6 AM (or the current hour, if later) to 11 PM, local time."""
    hrs = om.get("hourly") or {}
    times = hrs.get("time") or []
    if not times:
        return []
    today = om["daily"]["time"][0]
    now = (om.get("current") or {}).get("time", "")
    start = max(first_hour, int(now[11:13])) if now[:10] == today and len(now) >= 13 else first_hour
    out = []
    for i, t in enumerate(times):
        if t[:10] != today or int(t[11:13]) < start:
            continue
        try:
            out.append({
                "hour": _hm(int(t[11:13])),
                "h24": int(t[11:13]),
                "temp": round(hrs["temperature_2m"][i]),
                "feels": round(hrs["apparent_temperature"][i]),
                "rain": hrs["precipitation_probability"][i] or 0,
                "conditions": WMO.get(hrs["weather_code"][i], "—"),
                "wind": round(hrs["wind_speed_10m"][i]),
            })
        except (KeyError, IndexError, TypeError):
            continue
    return out


def report(name, lat, lon, cfg, us=True):
    ua = cfg["user_agent"]
    box = {"name": name, "ok": False, "errors": []}
    try:
        om = _open_meteo(lat, lon, ua)
        cur, daily = om["current"], om["daily"]
        box.update({
            "ok": True,
            "now": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]),
            "conditions": WMO.get(cur["weather_code"], "—"),
            "wind": round(cur["wind_speed_10m"]),
            "humidity": round(cur["relative_humidity_2m"]),
            "high": round(daily["temperature_2m_max"][0]),
            "low": round(daily["temperature_2m_min"][0]),
            "rain": daily["precipitation_probability_max"][0],
            "today": WMO.get(daily["weather_code"][0], "—"),
            "sunrise": daily["sunrise"][0][-5:],
            "sunset": daily["sunset"][0][-5:],
            "hours": hourly(om),
            "days": [
                {"date": daily["time"][i], "high": round(daily["temperature_2m_max"][i]),
                 "low": round(daily["temperature_2m_min"][i]),
                 "conditions": WMO.get(daily["weather_code"][i], "—"),
                 "rain": daily["precipitation_probability_max"][i]}
                for i in range(1, min(4, len(daily["time"])))
            ],
        })
    except Exception as exc:
        box["errors"].append(f"Open-Meteo: {exc}"[:200])
    if us:
        try:
            box.update({"forecast": None, "alerts": []})
            nws = _nws(lat, lon, ua)
            box["forecast"], box["alerts"] = nws["text"], nws["alerts"]
            box["ok"] = box["ok"] or bool(nws["text"])
        except Exception as exc:
            box["errors"].append(f"NWS: {exc}"[:200])
    return box
