"""Current temperature from Open-Meteo (free, no API key)."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "splitflap-web"


def _get(url: str, params: dict, timeout: float = 8) -> dict:
    request = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as e:
        raise ValueError("weather service not reachable (%s)" % getattr(e, "reason", e))


def find_place(query: str, language: str = "en") -> dict:
    """Look a place up by name, or take "47.37, 8.54" as coordinates."""
    query = (query or "").strip()
    if not query:
        raise ValueError("enter a place")
    match = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$", query)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("coordinates out of range")
        return {"name": "%.3f, %.3f" % (lat, lon), "region": "", "latitude": lat, "longitude": lon}
    data = _get(GEOCODING_URL, {"name": query, "count": 1, "language": language, "format": "json"})
    results = data.get("results") or []
    if not results:
        raise ValueError("no place called %r found" % query)
    r = results[0]
    region = ", ".join(x for x in (r.get("admin1"), r.get("country")) if x and x != r.get("name"))
    return {"name": r["name"], "region": region,
            "latitude": float(r["latitude"]), "longitude": float(r["longitude"])}


def current_temperature(latitude: float, longitude: float) -> float:
    data = _get(FORECAST_URL, {"latitude": latitude, "longitude": longitude,
                               "current": "temperature_2m", "timezone": "auto"})
    try:
        return float(data["current"]["temperature_2m"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("unexpected answer from the weather service")


def format_temperature(value: float, fmt: str = "{temp}C") -> str:
    temp = int(round(value))
    return fmt.format(temp=temp if temp != 0 else 0)
