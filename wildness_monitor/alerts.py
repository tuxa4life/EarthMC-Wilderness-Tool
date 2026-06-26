"""Outbound Discord webhook alerts — async, non-blocking, live-updating.

To keep the channel from flooding when a player sits in wilderness, each player's
wilderness stay is one **session** = one Discord message that is *edited in place*:

    enters wilderness → POST a new message (loud, may @Mercenary)   [action NEW]
    still in wilderness → PATCH that same message with fresh coords  [action EDIT, silent]
    leaves / logs off   → PATCH a final "left wilderness" summary     [action EDIT_FINAL]

So a target that stands in wilderness for 10 minutes produces ONE message that
updates every cycle, not 20 near-duplicate pings. A *new* notification (and the
role mention) only happens on a fresh entry.

Threading model is unchanged: the main ping loop *builds* the payload (including
the time-windowed @Mercenary decision, which must stay ordered/on-time) and calls
`_enqueue()`, which is instant. A single daemon worker thread drains the queue and
does the actual POST/PATCH. Keeping one worker preserves message order, avoids
Discord rate limits, and means `_session` and the `_message_ids` map (session key
→ Discord message id) are only ever touched by that one thread.

`_sessions` (main-thread state) tracks which players currently have an open
wilderness message, the data needed for the final summary, and the per-session
cycle counter that drives the @Mercenary escalation — read/written only on the
main thread.
"""
import time
import queue
import threading
import requests

from wildness_monitor import config, towns
from wildness_monitor.logkit import log, log_error, format_duration
from wildness_monitor.config import (
    DISCORD_WEBHOOK_URL, MERCENARY_ROLE_ID, PING_ROLE_THRESHOLD,
    SQUAD_RADIUS, WEBHOOK_TIMEOUT, WEBHOOK_RETRIES, WEBHOOK_RETRY_BACKOFF,
    WEBHOOK_QUEUE_MAX,
)

_session = requests.Session()

# Discord message flags. SUPPRESS_EMBEDS keeps the map link from expanding into a
# big embed; SUPPRESS_NOTIFICATIONS makes a post silent (no phone buzz) — used for
# updates/finals so only a fresh entry actually notifies.
_SUPPRESS_EMBEDS = 1 << 2          # 4
_SILENT = 1 << 12                  # 4096

# Queue actions.
_NEW, _EDIT, _EDIT_FINAL, _STATUS = "new", "edit", "final", "status"
_GONE = object()                   # sentinel: a PATCH target 404'd (message deleted)

# Main-thread state.
_sessions: dict[str, dict] = {}    # name_lower → {entry_ts, x, z, name, cycles, escalated}

# Worker-thread state.
_message_ids: dict[str, str] = {}  # name_lower → Discord message id

# Background delivery queue. Each item is (action, key, payload, label); key is the
# session name (None for status messages), label is used only for error logging.
_queue: "queue.Queue[tuple[str, str | None, dict, str]]" = queue.Queue(maxsize=WEBHOOK_QUEUE_MAX)
_worker_started = False
_worker_lock = threading.Lock()

_WAIT_URL = f"{DISCORD_WEBHOOK_URL}?wait=true" if DISCORD_WEBHOOK_URL else ""


# ── Background sender ───────────────────────────────────────────────────────

def _ensure_worker():
    """Start the delivery thread on first use (idempotent, thread-safe)."""
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker_loop, daemon=True, name="discord-webhook").start()
        _worker_started = True


def _enqueue(action: str, key: "str | None", payload: dict, label: str):
    """Hand a prepared item to the background worker. Never blocks the caller."""
    if not DISCORD_WEBHOOK_URL:
        return
    _ensure_worker()
    try:
        _queue.put_nowait((action, key, payload, label))
    except queue.Full:
        log_error(f"Discord webhook queue full — dropped message for {label}")


def _worker_loop():
    while True:
        item = _queue.get()
        try:
            _process(*item)
        finally:
            _queue.task_done()


def _process(action: str, key: "str | None", payload: dict, label: str):
    """Apply one queued item: post a new message, edit an existing one, or finalize."""
    if action == _STATUS:
        _deliver("post", DISCORD_WEBHOOK_URL, payload, label)
        return

    if action == _NEW:
        data = _deliver("post", _WAIT_URL, payload, label)
        if isinstance(data, dict) and data.get("id"):
            _message_ids[key] = data["id"]
        return

    # EDIT / EDIT_FINAL — patch the session's message; repost if it's gone.
    mid = _message_ids.get(key)
    if mid is not None:
        result = _deliver("patch", f"{DISCORD_WEBHOOK_URL}/messages/{mid}", payload, label)
        if result is _GONE:
            mid = None                     # message deleted → fall through to repost
    if mid is None:
        data = _deliver("post", _WAIT_URL, payload, label)
        if isinstance(data, dict) and data.get("id") and action == _EDIT:
            _message_ids[key] = data["id"]
    if action == _EDIT_FINAL:
        _message_ids.pop(key, None)        # session closed; next entry starts fresh


def _deliver(verb: str, url: str, payload: dict, label: str):
    """POST/PATCH one payload with retry. Runs only on the worker thread.

    Returns the response JSON (dict, possibly empty) on success, `_GONE` if a PATCH
    target no longer exists (404 → caller reposts), or None on give-up.
    """
    fn = _session.post if verb == "post" else _session.patch
    for attempt in range(WEBHOOK_RETRIES + 1):
        try:
            resp = fn(url, json=payload, timeout=WEBHOOK_TIMEOUT)
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                return {}
        except requests.exceptions.HTTPError as e:
            r = getattr(e, "response", None)
            if verb == "patch" and r is not None and r.status_code == 404:
                return _GONE
            backoff = _retry_after(e)
            if attempt < WEBHOOK_RETRIES and backoff is not None:
                time.sleep(backoff)
                continue
            log_error(f"ERROR sending Discord webhook for {label}: {e}")
            return None
        except Exception as e:
            backoff = _retry_after(e)
            if attempt < WEBHOOK_RETRIES and backoff is not None:
                time.sleep(backoff)
                continue
            log_error(f"ERROR sending Discord webhook for {label}: {e}")
            return None
    return None


def _retry_after(e: Exception) -> float | None:
    """Seconds to wait if this is a retryable error, else None (give up).

    Retryable: read/connect timeouts, connection errors, HTTP 429 and 5xx.
    Honors Discord's Retry-After header on 429.
    """
    if isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return WEBHOOK_RETRY_BACKOFF
    if isinstance(e, requests.exceptions.HTTPError):
        resp = getattr(e, "response", None)
        code = resp.status_code if resp is not None else 0
        if code == 429:
            try:
                return float(resp.headers.get("Retry-After", WEBHOOK_RETRY_BACKOFF))
            except (TypeError, ValueError):
                return WEBHOOK_RETRY_BACKOFF
        if code >= 500:
            return WEBHOOK_RETRY_BACKOFF
    return None


def flush_webhooks(timeout: float = 5.0):
    """Best-effort drain of the queue before shutdown so final messages still send."""
    if not _worker_started:
        return
    done = threading.Event()
    threading.Thread(target=lambda: (_queue.join(), done.set()), daemon=True).start()
    if not done.wait(timeout):
        log("Discord webhook flush timed out — some messages may not have sent.")


# ── Message building ────────────────────────────────────────────────────────

def _map_link(coords: str, x: int, z: int) -> str:
    return f"**[{coords}]({config.map_url(x, z)})**"


def _town_row(t) -> str:
    if t.get("is_capital"):
        # Nation /n spawn — distinguished by underline alone (no icon, no label)
        # from the plain bold used for /t spawn towns.
        label = t.get("nation") or t["name"]
        return f"__**{label}**__ {t['distance']} blocks  {t['direction']}"
    return f"**{t['name']}** {t['distance']} blocks  {t['direction']}"


def _town_block(towns) -> str:
    return "\n" + "\n".join(_town_row(t) for t in towns) if towns else ""


def _detail_blocks(towns, heading, dwell, nearby, nearby_allies) -> str:
    """The towns / movement / squad lines shared by entry and update messages."""
    town_str = _town_block(towns)

    move_parts = []
    if heading:
        move_parts.append(f"Moving **{heading}**")
    dwell_secs = int(dwell)
    if dwell_secs > 0:
        move_parts.append(f"in wilderness **{format_duration(dwell_secs)}**")
    move_str = f"\n{' · '.join(move_parts)}" if move_parts else ""

    squad_str = ""
    if nearby > 0:
        ally_str = f" ({nearby_allies} Georgian)" if nearby_allies else ""
        squad_str = f"\n⚠ **{nearby}** player(s) within {SQUAD_RADIUS} blocks{ally_str}"

    return f"{town_str}{move_str}{squad_str}"


# ── Public API (called from the ping loop / orchestration) ──────────────────

def send_discord_status(message: str):
    _enqueue(_STATUS, None, {"content": message}, "status")


def send_discord_alert(
    player_name: str, coords: str, x: int, z: int, is_new_player: bool,
    towns: list[dict] | None = None,
    heading: str | None = None, dwell: float = 0.0, nearby: int = 0, nearby_allies: int = 0,
):
    """Open or update a player's live wilderness message. New players are skipped.

    Also drives the @Mercenary escalation: each call while the player stays in
    wilderness bumps a per-session cycle counter, and once it reaches
    PING_ROLE_THRESHOLD a single loud role ping is posted (see below).
    """
    if not DISCORD_WEBHOOK_URL or is_new_player:
        return

    key = player_name.lower()
    session = _sessions.get(key)
    is_entry = session is None
    now = int(time.time())

    if is_entry:
        session = _sessions[key] = {
            "entry_ts": now, "x": x, "z": z, "name": player_name,
            "cycles": 1, "escalated": False, "towns": towns,
        }
    else:
        session["x"], session["z"] = x, z
        session["cycles"] += 1
        session["towns"] = towns
    entry_ts = session["entry_ts"]

    details = _detail_blocks(towns, heading, dwell, nearby, nearby_allies)
    link = _map_link(coords, x, z)

    if is_entry:
        # Loud (notifies), but no role mention — the @Mercenary ping is reserved for
        # targets that *stay* exposed (handled by the escalation below).
        content = f"➡ <t:{entry_ts}:T> - `{player_name}` entered wilderness at {link}{details}"
        _enqueue(_NEW, key, {"content": content, "flags": _SUPPRESS_EMBEDS}, player_name)
    else:
        # Silent in-place edit — no new notification, always-current coords.
        content = (
            f"➡ <t:{entry_ts}:T> - `{player_name}` in wilderness at {link} "
            f"· updated <t:{now}:R>{details}"
        )
        _enqueue(_EDIT, key, {"content": content, "flags": _SUPPRESS_EMBEDS | _SILENT}, player_name)

    # @Mercenary escalation: once a target has been continuously in wilderness for
    # PING_ROLE_THRESHOLD cycles, fire ONE loud role ping. It's a separate message
    # because editing the (silent) live message would never notify anyone. Fires at
    # most once per session — `escalated` is reset when the session ends.
    if (MERCENARY_ROLE_ID and not session["escalated"]
            and session["cycles"] >= PING_ROLE_THRESHOLD):
        session["escalated"] = True
        held = format_duration(max(0, now - entry_ts))
        esc = (f"<@&{MERCENARY_ROLE_ID}> `{player_name}` has held wilderness "
               f"**{held}** — still exposed at {link}")
        _enqueue(_STATUS, None, {"content": esc, "flags": _SUPPRESS_EMBEDS}, player_name)


def _departure_reason(key: str, online_positions: dict[str, tuple[int, int]]) -> str:
    """Why the player left wilderness, for the final summary.

    Still online → resolve their current position against the local town index
    (all towns, Georgian included — independent of the teleport cache) and name the
    town they walked into. Gone from players.json → "no longer visible on the map"
    (EarthMC can't tell a logoff from hiding under blocks, so we don't assert).
    """
    pos = online_positions.get(key)
    if pos is None:
        return "no longer visible on the map"
    town = towns.town_at(*pos)
    return f"entered **{town}**" if town else "entered a town"


def finalize_absent_sessions(active_keys: set[str],
                             online_positions: dict[str, tuple[int, int]] | None = None):
    """Close out any open session whose player is no longer in wilderness.

    Called once per FULL cycle with the lowercase names currently in wilderness and
    a map of every online monitored player's current (x, z). Each closed session
    gets one final silent edit summarizing the stay — including *why* it ended (which
    town they entered, or that they dropped off the map) — then is forgotten so a
    later re-entry starts a fresh message. MAIN THREAD ONLY.
    """
    if not DISCORD_WEBHOOK_URL:
        return
    online_positions = online_positions or {}
    now = int(time.time())
    for key in [k for k in _sessions if k not in active_keys]:
        s = _sessions.pop(key)
        dwell = format_duration(max(0, now - s["entry_ts"]))
        link = _map_link(f"({s['x']}, {s['z']})", s["x"], s["z"])
        reason = _departure_reason(key, online_positions)
        content = (
            f"⬅ <t:{s['entry_ts']}:T> - `{s['name']}` left wilderness after "
            f"**{dwell}** — last seen {link} · {reason}{_town_block(s.get('towns'))}"
        )
        _enqueue(_EDIT_FINAL, key, {"content": content, "flags": _SUPPRESS_EMBEDS | _SILENT}, s["name"])
