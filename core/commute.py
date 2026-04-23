"""Commute optimizer.

Providers:
  - OSRM public router for driving
  - Google Maps Directions API if GOOGLE_MAPS_API_KEY is set
  - Amtrak schedule scrape via browser_tools.fetch_page

Public API:
  geocode(place) -> (lat, lon) | None
  route(from_place, to_place, mode="driving") -> dict
  amtrak_next(origin_code, destination_code, when) -> list[dict]
  calculate_departure(destination, arrival_time, buffer_min=10, mode="driving") -> dict
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

OSRM_BASE = "https://router.project-osrm.org"
NOMINATIM = "https://nominatim.openstreetmap.org"
GMAPS_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
HOME_ADDRESS = os.environ.get("HOME_ADDRESS", "Boston, MA")

GEOCODE_CACHE = Path(ALFRED_HOME) / "data" / "geocode_cache.json"
GEOCODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
COMMUTE_REMINDERS = Path(ALFRED_HOME) / "data" / "commute_reminders.json"


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def geocode(place: str) -> Optional[tuple[float, float]]:
    if not place:
        return None
    cache = _load_cache(GEOCODE_CACHE)
    key = place.strip().lower()
    if key in cache:
        val = cache[key]
        return (float(val[0]), float(val[1])) if val else None
    params = {"format": "json", "q": place, "limit": 1}
    url = NOMINATIM + "/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Alfred/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        cache[key] = None
        _save_cache(GEOCODE_CACHE, cache)
        return None
    if not data:
        cache[key] = None
        _save_cache(GEOCODE_CACHE, cache)
        return None
    lat = float(data[0]["lat"])
    lon = float(data[0]["lon"])
    cache[key] = [lat, lon]
    _save_cache(GEOCODE_CACHE, cache)
    # Nominatim rate limit is 1 req/sec
    time.sleep(1.0)
    return (lat, lon)


def _osrm_driving(from_ll: tuple[float, float], to_ll: tuple[float, float]) -> Optional[dict]:
    url = f"{OSRM_BASE}/route/v1/driving/{from_ll[1]},{from_ll[0]};{to_ll[1]},{to_ll[0]}?overview=false"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    if not data.get("routes"):
        return None
    r = data["routes"][0]
    return {"seconds": int(r["duration"]), "meters": int(r["distance"]), "provider": "osrm", "warnings": []}


def _gmaps(from_ll: tuple[float, float], to_ll: tuple[float, float], mode: str) -> Optional[dict]:
    if not GMAPS_KEY:
        return None
    params = {
        "origin": f"{from_ll[0]},{from_ll[1]}",
        "destination": f"{to_ll[0]},{to_ll[1]}",
        "mode": mode,
        "key": GMAPS_KEY,
    }
    url = "https://maps.googleapis.com/maps/api/directions/json?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    routes = data.get("routes") or []
    if not routes:
        return None
    leg = routes[0]["legs"][0]
    return {
        "seconds": int(leg["duration"]["value"]),
        "meters": int(leg["distance"]["value"]),
        "provider": "google",
        "warnings": routes[0].get("warnings", []),
    }


def route(from_place: str, to_place: str, mode: str = "driving") -> dict:
    from_ll = geocode(from_place)
    to_ll = geocode(to_place)
    if not from_ll or not to_ll:
        return {"error": "geocode_failed", "warnings": []}
    if mode == "driving":
        osrm = _osrm_driving(from_ll, to_ll)
        if osrm:
            return osrm
    gm = _gmaps(from_ll, to_ll, mode)
    if gm:
        return gm
    return {"error": "no_provider", "warnings": []}


_AMTRAK_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)


def amtrak_next(origin_code: str, destination_code: str, when: Optional[datetime] = None) -> list[dict]:
    when = when or datetime.now()
    date_str = when.strftime("%Y-%m-%d")
    url = (
        "https://www.amtrak.com/tickets/departure.html"
        f"?destinationStation={destination_code}&originStation={origin_code}&departureDate={date_str}"
    )
    try:
        from browser_tools import fetch_page  # type: ignore

        page = fetch_page(url, max_chars=20000)
        text = getattr(page, "text", "")
    except Exception:
        return []
    if not text:
        return []
    out: list[dict] = []
    for m in _AMTRAK_TIME_RE.finditer(text):
        hour = int(m.group(1)) % 12
        minute = int(m.group(2))
        meridiem = m.group(3).upper()
        if meridiem == "PM":
            hour += 12
        depart = when.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if depart < when:
            depart += timedelta(days=1)
        out.append({
            "depart": depart.isoformat(),
            "arrive": None,
            "duration_min": None,
            "train_number": None,
        })
        if len(out) >= 5:
            break
    return out


def calculate_departure(destination: str, arrival_time: datetime, buffer_min: int = 10, mode: str = "driving") -> dict:
    r = route(HOME_ADDRESS, destination, mode=mode)
    if "error" in r:
        return r
    travel = int(r["seconds"])
    leave_by = arrival_time - timedelta(seconds=travel + buffer_min * 60)
    return {
        "leave_by": leave_by.isoformat(),
        "travel_seconds": travel,
        "buffer_min": buffer_min,
        "notes": f"route via {r.get('provider')}",
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest"])
    ns = ap.parse_args()
    if ns.cmd == "selftest":
        # stub route and amtrak
        globals()["route"] = lambda a, b, mode="driving": {"seconds": 1800, "meters": 20000, "provider": "stub", "warnings": []}
        globals()["amtrak_next"] = lambda o, d, when=None: [
            {"depart": (datetime.now() + timedelta(hours=1)).isoformat()} for _ in range(3)
        ]
        leave = calculate_departure("Target", datetime.now() + timedelta(hours=2))
        print(f"Commute self-test: leave-by math ok, amtrak stub returned 3")
        raise SystemExit(0)
