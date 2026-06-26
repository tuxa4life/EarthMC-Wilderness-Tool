"""Open-towns cache: which towns an Iberian player can teleport to.

Two-phase build (see CLAUDE.md for the why):
    Phase 1 — markers.json: fast, parses isPublic=true towns + polygon centroids.
    Phase 2 — v4/towns API: verifies canOutsidersSpawn and swaps in real spawn coords.

`_open_towns_cache` is reassigned wholesale by `refresh_open_towns_cache` (called
from a background thread). All reads go through `get_open_towns` /
`find_closest_open_towns` in this module, so the swap is atomic to callers.
"""
import re
import math
import heapq
import requests

from wildness_monitor.http import fetch
from wildness_monitor.logkit import log
from wildness_monitor.geometry import heading
from wildness_monitor.config import (
    MARKERS_URL, TOWNS_API_URL, NATIONS_API_URL, EXCLUDED_NATIONS, ALLY_NATIONS,
    TOWNS_BATCH_SIZE, TOWNS_BATCH_DELAY, TOWNS_IN_ALERT, NATIONS_BATCH_SIZE,
)
import time

_open_towns_cache: list[dict] = []

# Local wilderness detection. A list of (minx, minz, maxx, maxz, rings) — one entry
# per town claim polygon, where `rings` is a list of vertex lists. `is_wilderness`
# answers "is (x, z) inside ANY town?" via point-in-polygon, so the hot detection
# path needs no /v4/location API call. None = not built yet (cold); the caller
# falls back to the API until the first build completes. Rebound wholesale by
# build_claims_index (background thread), so reads are atomic — same pattern as
# _open_towns_cache. See CLAUDE.md "Local wilderness detection".
_claims_index: "list[tuple[int, int, int, int, list, str]] | None" = None


def _point_in_ring(x: float, z: float, ring: list) -> bool:
    """Ray-casting point-in-polygon test for a single ring of (x, z) vertices."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, zi = ring[i]
        xj, zj = ring[j]
        if (zi > z) != (zj > z) and x < (xj - xi) * (z - zi) / (zj - zi) + xi:
            inside = not inside
        j = i
    return inside


def _claim_from_marker(marker: dict):
    """Extract a town's claim polygons + bounding box + name from one polygon marker.

    markers.json nests vertices as points[ring][part][vertex]; we treat each
    innermost vertex list as one polygon ring. The tooltip carries the town name
    as the first <b>…</b> span. Returns (minx, minz, maxx, maxz, rings, name) or
    None if the marker has no usable geometry.
    """
    rings: list[list[tuple[int, int]]] = []
    minx = minz = float("inf")
    maxx = maxz = float("-inf")
    for ring in marker.get("points", []):
        for part in ring:
            poly = []
            for pt in part:
                x, z = pt["x"], pt["z"]
                poly.append((x, z))
                minx, maxx = min(minx, x), max(maxx, x)
                minz, maxz = min(minz, z), max(maxz, z)
            if len(poly) >= 3:
                rings.append(poly)
    if not rings:
        return None
    name_m = re.search(r'<b>([^<]+)</b>', marker.get("tooltip", ""))
    name = name_m.group(1).strip() if name_m else ""
    return (minx, minz, maxx, maxz, rings, name)


def _parse_all_claims_from_markers(markers_data: list) -> list:
    """Every town claim polygon (no public/nation filtering) for wilderness detection."""
    towny_layer = next((l for l in markers_data if l.get("id") == "towny"), None)
    if not towny_layer:
        return []
    claims = []
    for marker in towny_layer.get("markers", []):
        if marker.get("type") != "polygon":
            continue
        claim = _claim_from_marker(marker)
        if claim:
            claims.append(claim)
    return claims


def build_claims_index(session: requests.Session) -> int:
    """Rebuild the local town-boundary index from markers.json. Returns claim count.

    Call from a background thread (it does a ~7.6 MB GET). Rebinds _claims_index
    atomically so the main loop's reads never see a half-built index.
    """
    global _claims_index
    resp = fetch(session.get, MARKERS_URL, timeout=30)
    resp.raise_for_status()
    _claims_index = _parse_all_claims_from_markers(resp.json())
    return len(_claims_index)


def is_wilderness(x: int, z: int) -> "bool | None":
    """True if (x, z) is in no town, False if inside one, None if the index is cold.

    A cheap bounding-box reject precedes each point-in-polygon test, so a query
    typically does only a handful of full polygon tests even with thousands of towns.
    """
    claims = _claims_index
    if claims is None:
        return None
    for minx, minz, maxx, maxz, rings, _name in claims:
        if x < minx or x > maxx or z < minz or z > maxz:
            continue
        for ring in rings:
            if _point_in_ring(x, z, ring):
                return False
    return True


def town_at(x: int, z: int) -> "str | None":
    """Name of the town containing (x, z), or None if (x, z) is wilderness or the
    index is cold.

    Covers ALL towns (Georgian and foreign alike) — it reads the same town-boundary
    index as is_wilderness, so it is independent of the open-towns *teleport* cache
    (which only holds outsider-spawnable towns). Returns None for an unnamed claim too.
    """
    claims = _claims_index
    if not claims:
        return None
    for minx, minz, maxx, maxz, rings, name in claims:
        if x < minx or x > maxx or z < minz or z > maxz:
            continue
        for ring in rings:
            if _point_in_ring(x, z, ring):
                return name or None
    return None


def claims_count() -> int:
    """Number of town claim areas in the boundary index (0 while cold)."""
    return len(_claims_index) if _claims_index is not None else 0


def _parse_open_towns_from_markers(markers_data: list) -> list[dict]:
    """Parse isPublic=true towns from squaremap markers.json, excluding EXCLUDED_NATIONS.

    These become the `/t spawn` candidates only. Nation `/n spawn` points are NOT
    derived from here anymore — they come from `_fetch_nation_spawns` (the v4/nations
    endpoint), which carries the real nation-spawn coordinate and public flag.

    Returns centroid coords as a fallback; actual spawn coords are filled in later by
    _verify_outsider_spawn once we hit the v4 API.
    """
    towny_layer = next((l for l in markers_data if l.get("id") == "towny"), None)
    if not towny_layer:
        return []

    towns = []
    for marker in towny_layer.get("markers", []):
        if marker.get("type") != "polygon":
            continue

        popup = marker.get("popup", "")
        tooltip = marker.get("tooltip", "")

        pub = re.search(r'Public:\s*<b>(true|false)</b>', popup, re.IGNORECASE)
        if not pub or pub.group(1).lower() != "true":
            continue

        name_m = re.search(r'<b>([^<]+)</b>', tooltip)
        if not name_m:
            continue
        town_name = name_m.group(1).strip()

        # Tooltip says "(Member of NationName)" for regular towns and
        # "(Capital of NationName)" for nation capitals — capture the nation either
        # way (needed for the EXCLUDED_NATIONS / ALLY_NATIONS checks). We no longer
        # special-case capitals here: a capital town is just another `/t spawn`
        # candidate if it allows outsider spawn; the `/n spawn` target is sourced
        # separately in _fetch_nation_spawns with the correct nation-spawn coords.
        nation_m = re.search(r'\((Member|Capital) of ([^)]+)\)', tooltip)
        nation_name = nation_m.group(2).strip() if nation_m else ""

        if nation_name.lower() in EXCLUDED_NATIONS:
            continue

        # Polygon centroid — good enough as a fallback if spawn coords are unavailable
        sx = sz = count = 0
        for ring in marker.get("points", []):
            for part in ring:
                for pt in part:
                    sx += pt["x"]
                    sz += pt["z"]
                    count += 1
        if not count:
            continue

        towns.append({
            "name": town_name,
            "nation": nation_name,
            "is_capital": False,        # /t spawn candidate; nation spawns added separately
            "x": int(sx / count),
            "z": int(sz / count),
        })

    return towns


def _verify_outsider_spawn(session: requests.Session, towns: list[dict]) -> list[dict]:
    """Keep only towns where canOutsidersSpawn=true, replacing centroid with actual spawn coords.

    ALLY_NATIONS towns bypass the canOutsidersSpawn check (Iberian residents can
    spawn to their own nation's public towns) but still get real spawn coords.

    Queries v4/towns in batches. Batches that fail are silently skipped; the next
    refresh will retry them.
    """
    name_to_town = {t["name"]: t for t in towns}
    names = list(name_to_town)
    verified = []

    for i in range(0, len(names), TOWNS_BATCH_SIZE):
        batch = names[i : i + TOWNS_BATCH_SIZE]
        try:
            resp = fetch(session.post, TOWNS_API_URL, json={"query": batch}, timeout=15)
            resp.raise_for_status()
            api_towns = resp.json()
            if not isinstance(api_towns, list):
                api_towns = [api_towns]
            for api_town in api_towns:
                name = api_town.get("name", "")
                base = name_to_town.get(name)
                if not base:
                    continue
                is_ally = base["nation"].lower() in ALLY_NATIONS
                if not is_ally and not api_town.get("status", {}).get("canOutsidersSpawn", False):
                    continue
                spawn = api_town.get("coordinates", {}).get("spawn", {})
                x = int(spawn.get("x", base["x"]))
                z = int(spawn.get("z", base["z"]))
                verified.append({**base, "x": x, "z": z})
        except Exception as e:
            log(f"WARNING: towns batch {i // TOWNS_BATCH_SIZE + 1} failed: {e}")
        if i + TOWNS_BATCH_SIZE < len(names):
            time.sleep(TOWNS_BATCH_DELAY)

    return verified


def _fetch_nation_spawns(session: requests.Session) -> list[dict]:
    """Fetch every publicly-reachable `/n spawn` point from the v4/nations endpoint.

    Nation spawns are a *separate* teleport option from town `/t spawn` and must
    not be inferred from the capital town: a nation's spawn is reachable by an
    outsider when the **nation's** `status.isPublic` is true (the `/n toggle public`
    flag), which is unrelated to the capital town's `canOutsidersSpawn`, and its
    coordinate is the nation's own `coordinates.spawn` — often hundreds of blocks
    from the capital's `/t spawn`. ALLY_NATIONS are always included (their own
    residents can `/n spawn` regardless of the public flag); EXCLUDED_NATIONS are
    skipped.

    Each returned entry is shaped like a town dict but flagged `is_capital=True`,
    with `name`/`nation` both set to the nation name so downstream renders it as a
    `/n spawn <nation>` target. Batches that fail are skipped (logged); the listing
    GET going down means no nation spawns this refresh (caller tolerates []).
    """
    resp = fetch(session.get, NATIONS_API_URL, timeout=20)
    resp.raise_for_status()
    names = [n["name"] for n in resp.json()]

    spawns: list[dict] = []
    for i in range(0, len(names), NATIONS_BATCH_SIZE):
        batch = names[i : i + NATIONS_BATCH_SIZE]
        try:
            r = fetch(session.post, NATIONS_API_URL, json={"query": batch}, timeout=20)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                data = [data]
            for nation in data:
                nation_name = nation.get("name", "")
                if not nation_name or nation_name.lower() in EXCLUDED_NATIONS:
                    continue
                is_public = nation.get("status", {}).get("isPublic", False)
                is_ally = nation_name.lower() in ALLY_NATIONS
                if not is_public and not is_ally:
                    continue
                spawn = nation.get("coordinates", {}).get("spawn", {})
                if "x" not in spawn or "z" not in spawn:
                    continue
                spawns.append({
                    "name": nation_name,
                    "nation": nation_name,
                    "is_capital": True,
                    "x": int(spawn["x"]),
                    "z": int(spawn["z"]),
                })
        except Exception as e:
            log(f"WARNING: nations batch {i // NATIONS_BATCH_SIZE + 1} failed: {e}")
        if i + NATIONS_BATCH_SIZE < len(names):
            time.sleep(TOWNS_BATCH_DELAY)

    return spawns


def refresh_open_towns_cache(session: requests.Session) -> list[dict]:
    """Rebuild the open-towns cache.

    Phase 1 — markers.json: fast, gets isPublic=true towns with polygon centroids.
    Phase 2 — v4/towns API: verifies canOutsidersSpawn and replaces coords with actual spawn point.
    Phase 3 — v4/nations API: adds publicly-reachable `/n spawn` points (is_capital=True).
    The cache mixes `/t spawn` towns and `/n spawn` nation points; find_closest_open_towns
    reserves one slot for the nearest nation spawn (see its docstring).
    """
    global _open_towns_cache

    resp = fetch(session.get, MARKERS_URL, timeout=30)
    resp.raise_for_status()
    candidates = _parse_open_towns_from_markers(resp.json())
    log(f"Verifying {len(candidates)} public towns for outsider spawn access...")

    verified = _verify_outsider_spawn(session, candidates)
    log(f"{len(verified)}/{len(candidates)} towns are teleport-accessible (outsider spawn or ally nation).")

    # Phase 3: nation /n spawn points (independent of the town pipeline — a failure
    # here leaves the town list intact rather than aborting the whole rebuild).
    try:
        nation_spawns = _fetch_nation_spawns(session)
        log(f"{len(nation_spawns)} public nation spawns added (/n spawn targets).")
    except Exception as e:
        log(f"WARNING: nation spawn fetch failed: {e} — no /n spawn targets this refresh.")
        nation_spawns = []

    # Drop a `/t spawn` town that sits exactly on a `/n spawn` point (a capital whose
    # town spawn == its nation spawn — e.g. Iberia/Mtskheta). It would otherwise
    # appear as a redundant second row at the same coords and waste a town slot.
    # Capitals whose `/t` and `/n` spawns differ are kept (both are useful targets).
    nation_pts = {(t["x"], t["z"]) for t in nation_spawns}
    verified = [t for t in verified if (t["x"], t["z"]) not in nation_pts]

    _open_towns_cache = verified + nation_spawns
    return _open_towns_cache


def get_open_towns() -> list[dict]:
    """Return the current cached open towns. Never blocks — empty if cache not yet built."""
    return _open_towns_cache


def find_closest_open_towns(
    x: int, z: int, n: int = TOWNS_IN_ALERT, session: requests.Session | None = None
) -> list[dict]:
    """Return the `n` closest teleport targets to (x, z), nearest first.

    One slot is reserved for the nearest **nation spawn** (`is_capital=True`,
    reachable via `/n spawn <nation>`, sourced from v4/nations with the real nation
    spawn coordinate); the remaining `n - 1` slots hold the nearest regular towns
    (`/t spawn`). If there's no nation spawn in range we fall back to all regular
    towns, and if there aren't enough regular towns we top up with more nation
    spawns. The final list is re-sorted nearest-first regardless of type.

    Each result is the town dict plus derived fields:
        distance   — blocks from (x, z) to the town's spawn.
        direction  — compass bearing of (x, z) *from* the town's origin, i.e. where
                     the player stands relative to that town (North = player is north
                     of the town spawn).
        is_capital — True if this entry is a nation spawn rather than a town spawn.
    Empty list if the cache is cold (and no session is passed to build it).

    Pass session only when a cold-cache build is acceptable (e.g. CLI). In the monitor,
    call without session so a cold cache returns [] instead of blocking for ~36 s.
    """
    towns = get_open_towns()
    if not towns and session is not None:
        towns = refresh_open_towns_cache(session)
    if not towns:
        return []

    annotated = [
        {
            **t,
            "distance": round(math.hypot(x - t["x"], z - t["z"])),
            "direction": heading(x - t["x"], z - t["z"]),
        }
        for t in towns
    ]

    # Only the nearest `n` of each kind can possibly make the final list, so pull
    # those with nsmallest (O(len·log n)) instead of fully sorting the whole cache.
    key = lambda t: t["distance"]
    capitals = heapq.nsmallest(n, (t for t in annotated if t.get("is_capital")), key=key)
    regular = heapq.nsmallest(n, (t for t in annotated if not t.get("is_capital")), key=key)

    # Reserve one slot for the nearest nation spawn, fill the rest with regular towns.
    cap_slots = 1 if capitals else 0
    chosen = capitals[:cap_slots] + regular[: n - cap_slots]
    # Top up from whichever pool still has entries if we came up short.
    if len(chosen) < n:
        leftovers = regular[len(chosen) - cap_slots:] + capitals[cap_slots:]
        chosen += leftovers[: n - len(chosen)]

    chosen.sort(key=key)
    return chosen
