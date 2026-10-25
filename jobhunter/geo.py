"""Commute from home: Nominatim (geocoding) + OSRM (drive time), both free and cached in the tracker.

OSRM's public server gives drive time without traffic. Usage policy: at most 1 request/second.
"""
import time
import urllib.parse

from jobhunter.http import request_json

_last_call = 0.0


def _polite():
    global _last_call
    wait = 1.1 - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def geocode(conn, query):
    """Return (lat, lon) for a place, or None. Cached forever."""
    row = conn.execute("SELECT lat, lon FROM geo_cache WHERE query = ?", (query,)).fetchone()
    if row:
        return (row["lat"], row["lon"]) if row["lat"] is not None else None
    _polite()
    url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=us&q=" + urllib.parse.quote(query)
    hits = request_json(url)
    lat, lon = (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else (None, None)
    conn.execute("INSERT OR REPLACE INTO geo_cache (query, lat, lon) VALUES (?, ?, ?)", (query, lat, lon))
    conn.commit()
    return (lat, lon) if lat is not None else None


def drive(conn, home, place):
    """Return (minutes, miles) from the home address to a place, or None."""
    key = f"{home}->{place}"
    row = conn.execute("SELECT minutes, miles FROM geo_cache WHERE query = ?", (key,)).fetchone()
    if row:
        return (row["minutes"], row["miles"]) if row["minutes"] is not None else None
    origin, dest = geocode(conn, home), geocode(conn, place)
    result = (None, None)
    if origin and dest:
        _polite()
        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}?overview=false")
        routes = request_json(url).get("routes")
        if routes:
            result = (round(routes[0]["duration"] / 60, 1), round(routes[0]["distance"] / 1609.34, 1))
    conn.execute("INSERT OR REPLACE INTO geo_cache (query, minutes, miles) VALUES (?, ?, ?)", (key, *result))
    conn.commit()
    return result if result[0] is not None else None
