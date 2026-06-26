"""Wilderness detection plus the per-player position / movement state.

Module-level caches (written only from the main thread — no locking, see CLAUDE.md):
    _position_cache  last known (x, z) per player (lowercase name)
    _wilderness_cache last known wilderness status per player
    _track            per-player {x, z, t, wild_since}: velocity baseline + dwell timer

All three are mutated in place and evicted on logoff. Never rebind them from
outside this module.
"""
import math
import time
import requests

from wildness_monitor.config import SQUAD_RADIUS, STATIONARY_MOVE_BLOCKS
from wildness_monitor.geometry import heading as _heading
from wildness_monitor.earthmc_api import (
    fetch_online_players, check_wilderness_batch, check_new_players_batch,
)
from wildness_monitor.towns import is_wilderness as _is_wilderness_local

_position_cache: dict[str, tuple[int, int]] = {}
_wilderness_cache: dict[str, bool] = {}
_track: dict[str, dict] = {}
# Cached new-player flag per name. Account age only ever crosses the 24 h line
# once, so an established player (False) is cached permanently (until logoff);
# only still-new players (True/unknown) are re-queried each cycle. This avoids a
# POST /v4/players on every 10 s cycle for the same wilderness targets.
_new_player_cache: dict[str, bool] = {}


def _resolve_new_players(session: requests.Session, names: list[str]) -> dict[str, bool]:
    """{lowercase_name: is_new} for `names`, querying only those not known-established."""
    to_query = [n for n in names if _new_player_cache.get(n.lower()) is not False]
    if to_query:
        fresh = check_new_players_batch(session, to_query)
        for n in to_query:
            _new_player_cache[n.lower()] = fresh.get(n.lower(), False)
    return {n.lower(): _new_player_cache.get(n.lower(), False) for n in names}


def _wilderness_flags(session: requests.Session, coords: list[tuple[int, int]]) -> list[bool]:
    """Wilderness flag per (x, z), in input order.

    Primary path is the local town-boundary index (point-in-polygon, no network).
    The /v4/location API is used only as a fallback while the index is still cold
    (startup, before the first markers.json build) — once warm, no API call is made.
    """
    local = [_is_wilderness_local(x, z) for x, z in coords]
    if all(f is not None for f in local):
        return local
    # Index cold for some/all coords — one API batch covers the cold ones.
    api = check_wilderness_batch(session, [[x, z] for x, z in coords])
    return [a if l is None else l for l, a in zip(local, api)]


def _count_nearby(
    key: str, pos: dict, online_map: dict[str, dict], resident_set: set[str]
) -> tuple[int, int]:
    """Count other online players within SQUAD_RADIUS of pos (same world only).

    Returns (total_nearby, nearby_residents) — the second value flags fellow
    nation members who may be acting as an escort.
    """
    nearby = nearby_allies = 0
    px, pz, pworld = pos["x"], pos["z"], pos.get("world")
    for other_key, opos in online_map.items():
        if other_key == key or opos.get("world") != pworld:
            continue
        if math.hypot(px - opos["x"], pz - opos["z"]) <= SQUAD_RADIUS:
            nearby += 1
            if other_key in resident_set:
                nearby_allies += 1
    return nearby, nearby_allies


def find_wilderness_residents(
    session: requests.Session,
    monitored: list[str],
    georgians: set[str] | None = None,
) -> tuple[int, list[dict], dict[str, tuple[int, int]]]:
    """Return (online_count, wilderness_list, online_positions) for the monitored names.

    `online_positions` maps each online monitored player's lowercase name to its
    current (x, z) this cycle — used by the caller to explain why a player left
    wilderness (which town they walked into vs. dropping off the map entirely).

    `monitored` is who to track — normally the Georgia residents plus any
    blacklisted players (who may not be Georgian), with the whitelist already
    subtracted by the caller (never-ping players aren't tracked at all, so they
    incur no position-cache or wilderness-check work). `georgians` is the lowercase
    set of *actual* nation members, used only to flag nearby allies in the squad
    line (defaults to all monitored names if omitted).

    Each cycle passes the complete monitored set, so a player missing from the
    online map has logged off and their caches are evicted at the end of the pass.

    Uses a position cache to skip the wilderness check for players whose
    coordinates haven't changed since the last cycle — their wilderness status
    is guaranteed identical as long as they haven't moved.

    Each wilderness entry is enriched (no extra API calls) with movement and
    proximity data drawn from players.json: the heading (direction of travel) since
    the last cycle, how long the player has been continuously in wilderness, and how
    many other online players are within SQUAD_RADIUS.
    """
    online_map = fetch_online_players(session)
    georgian_set = georgians if georgians is not None else {name.lower() for name in monitored}

    online = [
        (name, online_map[lower])
        for name in monitored
        if (lower := name.lower()) in online_map
    ]
    if not online:
        return 0, [], {}

    needs_check: list[tuple[str, dict]] = []
    still_wild: list[tuple[str, dict]] = []

    for name, pos in online:
        key = name.lower()
        curr = (pos["x"], pos["z"])
        if _position_cache.get(key) == curr and key in _wilderness_cache:
            if _wilderness_cache[key]:
                still_wild.append((name, pos))
            # else: cached as in-town, no action needed
        else:
            needs_check.append((name, pos))
        _position_cache[key] = curr

    # Wilderness check only for players whose position changed or are new. Resolved
    # locally from the town-boundary index (no API call) — see _wilderness_flags.
    newly_wild: list[tuple[str, dict]] = []
    if needs_check:
        coords = [(pos["x"], pos["z"]) for _, pos in needs_check]
        flags = _wilderness_flags(session, coords)
        for (name, pos), wild in zip(needs_check, flags):
            _wilderness_cache[name.lower()] = wild
            if wild:
                newly_wild.append((name, pos))

    in_wilderness = still_wild + newly_wild
    now = time.time()
    online_keys = {name.lower() for name, _ in online}
    wild_keys = {name.lower() for name, _ in in_wilderness}

    # Enrich each wilderness resident with movement/dwell/cluster data (reads the
    # previous track entry, which still holds last cycle's position + timestamp).
    enriched: list[dict] = []
    if in_wilderness:
        new_player_map = _resolve_new_players(session, [n for n, _ in in_wilderness])
        for name, pos in in_wilderness:
            key = name.lower()
            prev = _track.get(key)

            # Direction of travel only (no speed): show a heading once the player has
            # moved at least STATIONARY_MOVE_BLOCKS since last cycle, else None.
            heading = None
            if prev is not None:
                dx, dz = pos["x"] - prev["x"], pos["z"] - prev["z"]
                if math.hypot(dx, dz) >= STATIONARY_MOVE_BLOCKS:
                    heading = _heading(dx, dz)

            wild_since = prev["wild_since"] if (prev and prev.get("wild_since")) else now
            nearby, nearby_allies = _count_nearby(key, pos, online_map, georgian_set)

            enriched.append({
                "name": name,
                "x": pos["x"],
                "y": pos.get("y"),
                "z": pos["z"],
                "is_new_player": new_player_map.get(key, False),
                "heading": heading,
                "dwell": now - wild_since,
                "nearby": nearby,
                "nearby_allies": nearby_allies,
            })

    # Update the movement track for every online player so next cycle has a
    # one-cycle-old velocity baseline. wild_since persists across cycles while a
    # player stays in wilderness, and resets to None once they re-enter a town.
    for name, pos in online:
        key = name.lower()
        prev = _track.get(key)
        if key in wild_keys:
            wild_since = prev["wild_since"] if (prev and prev.get("wild_since")) else now
        else:
            wild_since = None
        _track[key] = {"x": pos["x"], "z": pos["z"], "t": now, "wild_since": wild_since}

    # Evict caches for players who logged off (online_keys reflects the whole
    # monitored set each cycle, so missing-from-online means offline).
    for key in _position_cache.keys() - online_keys:
        del _position_cache[key]
        _wilderness_cache.pop(key, None)
        _new_player_cache.pop(key, None)
    for key in _track.keys() - online_keys:
        del _track[key]

    online_positions = {name.lower(): (pos["x"], pos["z"]) for name, pos in online}
    return len(online), enriched, online_positions


def find_player(name: str, session: requests.Session | None = None) -> dict | None:
    """One-shot lookup of a single player's wilderness status (used by the CLI)."""
    sess = session or requests.Session()
    online_map = fetch_online_players(sess)
    pos = online_map.get(name.lower())
    if pos is None:
        return None

    wilderness = _wilderness_flags(sess, [(pos["x"], pos["z"])])[0]

    is_new = False
    if wilderness:
        new_map = check_new_players_batch(sess, [name])
        is_new = new_map.get(name.lower(), False)

    return {"is_wilderness": wilderness, "x": pos["x"], "y": pos.get("y"),
            "z": pos["z"], "is_new_player": is_new}
