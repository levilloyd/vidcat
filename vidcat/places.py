"""Place names for GPS coordinates ("Lighthouse Field, Santa Cruz, California, United States"), looked up with
OpenStreetMap's Nominatim service and cached in the catalog."""
import sqlite3
import threading
import time

import httpx

from . import __version__

NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = f"vidcat/{__version__} (home video catalog)"  # Nominatim's policy requires an identifying agent

# Cache key precision: 3 decimals is about 100 m, so clips filmed in the same spot share one lookup.
_KEY_SCALE = 1000
# Features whose name says nothing a town name doesn't ("Regent Road", "Alameda County").
_UNNAMED_CATEGORIES = {"highway", "boundary", "place", "landuse", "railway"}
_LOCALITY_KEYS = ("city", "town", "village", "hamlet", "municipality", "suburb", "county")

_lock = threading.Lock()
_last_request = 0.0


class PlaceUnavailable(RuntimeError):
    pass


def _round(x: float) -> int:
    """Round half away from zero, like SQLite's round(), so PLACE_SQL finds the same cache row."""
    return int(abs(x) + 0.5) * (1 if x >= 0 else -1)


def _key(lat: float, lon: float) -> tuple[int, int]:
    return _round(lat * _KEY_SCALE), _round(lon * _KEY_SCALE)


# The cached place name for a video row `v`, as a SELECT expression: NULL = not looked up yet, '' = nothing there.
PLACE_SQL = (
    f"(SELECT name FROM places WHERE lat_key = CAST(round(v.latitude * {_KEY_SCALE}) AS INTEGER) "
    f"AND lon_key = CAST(round(v.longitude * {_KEY_SCALE}) AS INTEGER))"
)


def describe(result: dict) -> str:
    """A short place name from a Nominatim reverse-geocoding result; "" if it found nothing (open sea)."""
    address = result.get("address") or {}
    parts = []
    if result.get("name") and result.get("category") not in _UNNAMED_CATEGORIES:
        parts.append(result["name"])
    locality = next((address[k] for k in _LOCALITY_KEYS if address.get(k)), None)
    for part in (locality, address.get("state"), address.get("country")):
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts)


def _lookup(lat: float, lon: float, language: str | None) -> str:
    global _last_request
    with _lock:  # Nominatim allows at most one request per second
        time.sleep(max(0.0, _last_request + 1.0 - time.monotonic()))
        try:
            r = httpx.get(
                NOMINATIM,
                params={"format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "zoom": 18},
                headers={"User-Agent": USER_AGENT, "Accept-Language": language or "en"},
                timeout=10,
            )
            r.raise_for_status()
            result = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PlaceUnavailable(f"Place lookup failed: {e}") from e
        finally:
            _last_request = time.monotonic()
    return "" if "error" in result else describe(result)


def place_name(conn: sqlite3.Connection, lat: float, lon: float, language: str | None = None) -> str | None:
    """The cached place name for these coordinates, looking it up first if needed. None if nothing is there.

    Raises PlaceUnavailable when the lookup service can't be reached (that isn't cached, so it's retried next time).
    """
    lat_key, lon_key = _key(lat, lon)
    row = conn.execute("SELECT name FROM places WHERE lat_key = ? AND lon_key = ?", (lat_key, lon_key)).fetchone()
    if row is None:
        name = _lookup(lat, lon, language)
        with conn:
            conn.execute("INSERT OR REPLACE INTO places (lat_key, lon_key, name) VALUES (?, ?, ?)",
                         (lat_key, lon_key, name))
    else:
        name = row["name"]
    return name or None


def unnamed_spots(conn: sqlite3.Connection) -> list[tuple[float, float]]:
    """One (latitude, longitude) per cache spot that cataloged videos were filmed at but has no saved place name."""
    spots: dict[tuple[int, int], tuple[float, float]] = {}
    rows = conn.execute(
        f"SELECT latitude, longitude FROM videos v WHERE latitude IS NOT NULL AND missing = 0 AND {PLACE_SQL} IS NULL "
        "ORDER BY created_at"
    )
    for lat, lon in rows:
        spots.setdefault(_key(lat, lon), (lat, lon))
    return list(spots.values())
