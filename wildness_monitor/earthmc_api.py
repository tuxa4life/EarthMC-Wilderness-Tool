"""Raw EarthMC API endpoints.

Thin wrappers that fetch and shape data — no caching, no business logic. Each
takes a `requests.Session` so the caller controls connection pooling.
"""
import time
import requests

from wildness_monitor.http import fetch
from wildness_monitor.config import PLAYERS_URL, LOCATION_API_URL, PLAYERS_API_URL


def fetch_online_players(session: requests.Session) -> dict[str, dict]:
    """Return {lowercase_name: {x, y, z, world}} for every player on the live map.

    players.json carries `y` (the player's actual block elevation) — kept so the
    mod can drop an accurately-placed Xaero's/JourneyMap waypoint. It may be absent
    for a player whose position the map hasn't resolved yet, hence `.get`.
    """
    resp = fetch(session.get, PLAYERS_URL, timeout=10)
    resp.raise_for_status()
    # players.json regularly carries entries with no position (vanished/hidden
    # players, or positions the map hasn't resolved). Skip anything missing a name
    # or x/z rather than KeyError-ing the whole cycle (which would look like an API
    # failure and could trip the false "API DOWN" state).
    online = {}
    for p in resp.json().get("players", []):
        name = p.get("name")
        if name is None or "x" not in p or "z" not in p:
            continue
        online[name.lower()] = {"x": p["x"], "y": p.get("y"), "z": p["z"], "world": p.get("world")}
    return online


def check_wilderness_batch(session: requests.Session, coords: list[list[int]]) -> list[bool]:
    """Return a wilderness flag per coordinate, in input order. Timeout 8 s."""
    payload = {"query": coords}
    resp = fetch(session.post, LOCATION_API_URL, json=payload, timeout=8)
    resp.raise_for_status()
    results = resp.json()
    if not isinstance(results, list):
        results = [results]
    # Default a malformed/odd element to False (not wilderness) so we never ping a
    # player into a town by accident.
    return [bool(r.get("isWilderness", False)) if isinstance(r, dict) else False for r in results]


def check_new_players_batch(session: requests.Session, names: list[str]) -> dict[str, bool]:
    """Return {lowercase_name: is_account_under_24h_old} for the given players."""
    payload = {"query": names}
    resp = fetch(session.post, PLAYERS_API_URL, json=payload, timeout=15)
    resp.raise_for_status()
    results = resp.json()
    if not isinstance(results, list):
        results = [results]
    now_ms = time.time() * 1000
    out = {}
    for p in results:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        registered = (p.get("timestamps") or {}).get("registered", 0)
        out[p["name"].lower()] = (now_ms - registered) / 3_600_000 < 24
    return out
