"""Georgia resident list: fetch from the API, persist to disk, refresh hourly.

`_residents` is the shared in-memory list. It is *rebound* (never mutated in
place) under `_residents_lock`, so a reader that grabs the current reference via
`get_residents()` holds a stable snapshot even if a refresh swaps in a new list.

This module also owns the whitelist/blacklist state (`_whitelist`, `_blacklist`):
loaded once at startup, then edited only from the main thread via `modify_list`
(driven by the command queue), and persisted to disk on every edit. See
CLAUDE.md "Whitelist / Blacklist".
"""
import time
import threading
from pathlib import Path
import requests

from wildness_monitor.http import fetch
from wildness_monitor import logkit
from wildness_monitor.config import (
    NATION_NAME, NATIONS_API_URL, RESIDENTS_FILE, RESIDENTS_REFRESH_INTERVAL,
    WHITELIST_FILE, BLACKLIST_FILE,
)

_residents_lock = threading.Lock()
_residents: list[str] = []
_session = requests.Session()

# Whitelist/blacklist sets (lowercase names). Both are loaded once at startup and
# thereafter only ever mutated from the MAIN thread (via the command queue), so —
# like the other single-writer caches — they need no lock. The bot never touches
# them directly. See CLAUDE.md "Whitelist / Blacklist".
_whitelist: set[str] = set()
_blacklist: set[str] = set()

# Temporary per-player mutes (/timeout): {lowercase name: expiry epoch seconds}.
# In-memory only — a restart clears them (see CLAUDE.md "Timeouts"). Written only
# from the main thread (command queue), like the whitelist/blacklist sets.
_timeouts: dict[str, float] = {}


def get_residents() -> list[str]:
    with _residents_lock:
        return _residents


def set_residents(residents: list[str]):
    global _residents
    with _residents_lock:
        _residents = residents


def fetch_georgia_residents() -> list[str]:
    payload = {"query": [NATION_NAME]}
    resp = fetch(_session.post, NATIONS_API_URL, json=payload, timeout=10)
    resp.raise_for_status()
    results = resp.json()
    nation = results[0] if isinstance(results, list) else results
    residents = nation.get("residents", [])
    if residents and isinstance(residents[0], dict):
        return [r["name"] for r in residents]
    return [str(r) for r in residents]


def _ensure_parent(path_str: str):
    """Create the containing directory (e.g. data/) if it doesn't exist yet."""
    Path(path_str).parent.mkdir(parents=True, exist_ok=True)


def save_residents(residents: list[str]):
    _ensure_parent(RESIDENTS_FILE)
    with open(RESIDENTS_FILE, "w") as f:
        f.write("\n".join(residents))


def load_residents() -> list[str]:
    path = Path(RESIDENTS_FILE)
    if not path.exists():
        return []
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def residents_refresh_loop():
    """Background thread: re-fetch the resident list every RESIDENTS_REFRESH_INTERVAL."""
    while True:
        time.sleep(RESIDENTS_REFRESH_INTERVAL)
        try:
            new_residents = fetch_georgia_residents()
            set_residents(new_residents)
            save_residents(new_residents)
        except Exception as e:
            logkit.log_error(f"ERROR fetching residents: {e}")


# ── Whitelist / blacklist ──────────────────────────────────────────────────

def _load_name_set(path_str: str) -> set[str]:
    """Load one-name-per-line file into a lowercase set. Missing file → created empty."""
    path = Path(path_str)
    if not path.exists():
        _ensure_parent(path_str)
        path.write_text("")          # create it so the file always exists on disk
        return set()
    with open(path) as f:
        return {line.strip().lower() for line in f if line.strip()}


def _save_name_set(path_str: str, names: set[str]):
    _ensure_parent(path_str)
    with open(path_str, "w") as f:
        f.write("\n".join(sorted(names)))


def load_filter_lists():
    """Load whitelist + blacklist into memory at startup. Call once, on the main thread."""
    global _whitelist, _blacklist
    _whitelist = _load_name_set(WHITELIST_FILE)
    _blacklist = _load_name_set(BLACKLIST_FILE)


def get_whitelist() -> set[str]:
    return _whitelist


def get_blacklist() -> set[str]:
    return _blacklist


def modify_list(which: str, action: str, name: str | None = None) -> str:
    """Apply a whitelist/blacklist edit and persist it. MAIN-THREAD ONLY.

    `which` is "whitelist" or "blacklist"; `action` is "add" / "remove" / "list".
    Mutates the in-memory set in place (never rebinds it) and rewrites the backing
    file immediately so the change survives a restart. Returns a human-readable reply.
    """
    names = _whitelist if which == "whitelist" else _blacklist
    file = WHITELIST_FILE if which == "whitelist" else BLACKLIST_FILE

    if action == "list":
        if not names:
            return f"The {which} is empty."
        return f"**{which}** ({len(names)}): " + ", ".join(f"`{n}`" for n in sorted(names))

    key = (name or "").strip().lower()
    if not key:
        return f"Usage: `!{which} {action} <name>`"

    if action == "add":
        if key in names:
            return f"`{key}` is already on the {which}."
        names.add(key)
        _save_name_set(file, names)
        return f"✅ Added `{key}` to the {which}."

    if action == "remove":
        if key not in names:
            return f"`{key}` is not on the {which}."
        names.discard(key)
        _save_name_set(file, names)
        return f"✅ Removed `{key}` from the {which}."

    return f"Unknown action `{action}` — use add / remove / list."


# ── Temporary timeouts (/timeout) ──────────────────────────────────────────

def get_timeouts() -> dict[str, float]:
    """Active timeouts {name: expiry}, pruning any that have elapsed. MAIN THREAD ONLY."""
    now = time.time()
    for key in [k for k, exp in _timeouts.items() if exp <= now]:
        del _timeouts[key]
    return _timeouts


def add_timeout(name: str | None, minutes: int) -> str:
    """Mute (or, with minutes<=0, un-mute) a player. MAIN-THREAD ONLY. Returns a reply.

    A timeout temporarily excludes the player from the monitored set, exactly like
    the whitelist but auto-expiring after `minutes`. In-memory only.
    """
    key = (name or "").strip().lower()
    if not key:
        return "Usage: `timeout <name> [minutes]` (0 minutes clears it)."
    if minutes <= 0:
        if _timeouts.pop(key, None) is not None:
            return f"✅ Cleared the timeout for `{key}` — pings re-enabled."
        return f"`{key}` has no active timeout."
    _timeouts[key] = time.time() + minutes * 60
    return f"⏲ `{key}` will not be pinged for **{minutes} min**."
