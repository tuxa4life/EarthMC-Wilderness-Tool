"""Orchestration: the main ping loop and the API-down state machine.

Owns the long-lived `requests.Session` used for EarthMC API calls, the
consecutive-failure / backoff state, and thread startup for the residents and
open-towns refresh loops.
"""
import atexit
import queue
import signal
import sys
import time
import threading
import requests

from wildness_monitor import config, logkit, residents, bot, broadcast
from wildness_monitor.http import fetch
from wildness_monitor.tracking import find_wilderness_residents, find_player
from wildness_monitor.towns import (
    find_closest_open_towns, refresh_open_towns_cache, get_open_towns,
    build_claims_index, claims_count,
)
from wildness_monitor.alerts import (
    send_discord_alert, send_discord_status, flush_webhooks, finalize_absent_sessions,
)

_session = requests.Session()
# Each background refresher gets its own session: requests.Session isn't guaranteed
# thread-safe, so the main loop's _session must not be shared with a thread doing a
# concurrent multi-MB GET. (_claims = hourly boundary index, _towns = open-towns cache.)
_claims_session = requests.Session()
_towns_session = requests.Session()
_api_was_down = False
_consecutive_failures = 0
_api_down_since: float | None = None
_current_backoff = config.API_DOWN_BACKOFF_START

# Inbound-command plumbing (see TODO feature B / CLAUDE.md "Command bot"). The
# bot thread only ever .put()s onto this queue; the main loop drains and applies
# it, so all shared-state writes stay on the main thread.
command_queue: "queue.Queue[bot.Command]" = queue.Queue()
_paused = False                  # /pause /resume — when True, alerts are silenced
_start_time = time.time()        # for /status uptime
_last_online = 0                 # residents online in the last cycle, for /status

# Lowercase names that were in wilderness (and alerted) on the previous cycle, so a
# player appearing here for the first time counts as a fresh "entry"/ping in the
# stats. Written only on the main thread.
_wild_last: set[str] = set()


def check_api_health() -> tuple[bool, str]:
    try:
        resp = fetch(_session.get, config.PLAYERS_URL, retries=0, timeout=8)
        resp.raise_for_status()
        return True, ""
    except Exception as e:
        return False, logkit.classify_error(e)


def claims_refresh_loop():
    """Rebuild the local town-boundary index (wilderness detection) every hour.

    The index lets the ping loop answer "in wilderness?" by point-in-polygon with
    no per-check API call. Built immediately on startup, then refreshed on the
    CLAIMS_REFRESH_INTERVAL cadence; a failure retries in 5 min (until the first
    build lands, the loop falls back to the /v4/location API — see _wilderness_flags).
    """
    while True:
        try:
            n = build_claims_index(_claims_session)
            logkit.log(f"Town boundary index ready: {n} claim areas (local wilderness detection).")
            time.sleep(config.CLAIMS_REFRESH_INTERVAL)
        except Exception as e:
            logkit.log_error(f"ERROR building town boundary index: {e} — retrying in 5 min.")
            time.sleep(300)


def open_towns_refresh_loop():
    while True:
        try:
            towns = refresh_open_towns_cache(_towns_session)
            logkit.log(f"Open towns cache ready: {len(towns)} towns accessible to Iberians.")
            time.sleep(config.OPEN_TOWNS_CACHE_TTL)
        except Exception as e:
            logkit.log_error(f"ERROR building open towns cache: {e} — retrying in 5 min.")
            time.sleep(300)


def _broadcast_event(r: dict, towns: list[dict], now: float) -> dict:
    """Build the mod-facing ping payload for one wilderness player.

    `towns` carries name/distance/direction so the mod can render each as a
    clickable "/t spawn <name>" suggestion; x/y/z let it drop a waypoint.
    """
    return {
        "type": "ping",
        "player": r["name"],
        "x": r["x"], "y": r["y"], "z": r["z"],
        "towns": [
            {"name": t["name"], "distance": t["distance"], "direction": t["direction"],
             # is_capital → mod should suggest `/n spawn <nation>` not `/t spawn`
             "is_capital": t.get("is_capital", False), "nation": t.get("nation", "")}
            for t in towns
        ],
        "heading": r["heading"],
        "dwell": int(r["dwell"]),
        "nearby": r["nearby"],
        "nearby_allies": r["nearby_allies"],
        "map_url": config.map_url(r["x"], r["z"]),
        "ts": int(now),
    }


def ping_all_residents():
    """Run one detection pass — every PING_CYCLE_INTERVAL (10 s).

    Scans ALL monitored players, updates each wilderness target's live Discord
    message (one edited-in-place message per session — see alerts.py), and mirrors
    every position to the broadcast/mod clients. Discord and broadcast now share the
    single 10 s cadence: because Discord alerts *edit* a per-target message rather
    than posting a new one each cycle, a 10 s refresh just keeps that one message
    current — no extra channel traffic — while the mod gets the same fresh position.
    """
    global _api_was_down, _consecutive_failures, _api_down_since, _current_backoff, _last_online, _wild_last

    res = residents.get_residents()
    whitelist = residents.get_whitelist()
    blacklist = residents.get_blacklist()
    timeouts = residents.get_timeouts()   # prunes expired; lowercase name → expiry

    # Monitored = (Georgia residents ∪ blacklist) − whitelist − active timeouts.
    # Blacklisted players are watched regardless of nation; whitelisted and
    # timed-out players are dropped here so they never even reach the position cache
    # or the wilderness check (whitelist wins over blacklist; a timeout is a
    # temporary whitelist). `georgians` is the actual-resident set, used only to
    # flag nearby allies — it intentionally keeps excluded members so they're still
    # counted as escorts via the online map.
    georgians = {name.lower() for name in res}
    excluded = whitelist | timeouts.keys()
    monitored = [r for r in res if r.lower() not in excluded]
    monitored += [b for b in blacklist if b not in georgians and b not in excluded]
    if not monitored:
        # Only the genuine "nothing loaded yet" case is worth a log line; an empty
        # monitored set because everyone is whitelisted/timed-out is normal — stay quiet.
        if not res and not blacklist:
            logkit.log("No residents or blacklist loaded yet — waiting for first fetch.")
        return

    try:
        online_count, results, online_positions = find_wilderness_residents(
            _session, monitored, georgians=georgians,
        )
    except Exception as e:
        error_desc = logkit.classify_error(e)
        _consecutive_failures += 1
        logkit.log_error(
            f"ERROR during batch check: {error_desc} "
            f"({_consecutive_failures}/{config.CONSECUTIVE_FAILURES_THRESHOLD})"
        )
        if _consecutive_failures >= config.CONSECUTIVE_FAILURES_THRESHOLD and not _api_was_down:
            _api_was_down = True
            _api_down_since = time.time()
            _current_backoff = config.API_DOWN_BACKOFF_START
            send_discord_status(f"🔴 **EarthMC API is DOWN** — {error_desc}")
        return

    _consecutive_failures = 0
    _last_online = online_count

    # While paused (/pause), detection keeps running so caches/state stay warm, but
    # no alerts are sent and the live-session set is left frozen until resume.
    if not _paused:
        now = time.time()
        current: set[str] = set()
        for r in results:
            coords = f"({r['x']}, {r['z']})"
            towns = find_closest_open_towns(r["x"], r["z"])
            send_discord_alert(
                r["name"], coords, r["x"], r["z"], r["is_new_player"], towns,
                heading=r["heading"], dwell=r["dwell"],
                nearby=r["nearby"], nearby_allies=r["nearby_allies"],
            )
            if not r["is_new_player"]:
                # Mirror the alert to game-chat mods (skips new players, same as
                # Discord). Skip building the payload if no mod is even connected.
                if broadcast.has_clients():
                    broadcast.broadcast(_broadcast_event(r, towns, now))
                key = r["name"].lower()
                current.add(key)
                # Count a "ping" only on a FRESH entry (not every 10 s cycle a player
                # stays out), so the summary reflects real alerts, not detection ticks.
                if key not in _wild_last:
                    logkit._stats["pings"] += 1
                    logkit._stats["pings_total"] += 1
                    logkit._stats["players"].add(r["name"])

        # Close out the live message of anyone who was in wilderness last cycle but
        # isn't now (left wilderness or logged off), then remember this cycle's set.
        finalize_absent_sessions(current, online_positions)
        _wild_last = current

    logkit._stats["cycles"] += 1
    logkit._stats["peak_online"] = max(logkit._stats["peak_online"], online_count)


# ── Inbound command handling ────────────────────────────────────────────────
# Fast, in-memory commands run inline on the main thread. Commands that make
# network calls (`/check`, `/reload`) are offloaded to a short-lived thread so
# they never stall the ping loop — mirroring the async webhook sender. Either way
# the handler sets `cmd.result` and the bot thread is unblocked via `cmd.done`.

_ASYNC_COMMANDS = {"check", "reload"}


def _drain_command_queue():
    """Dispatch queued bot commands. Called at the top of each main-loop pass."""
    while True:
        try:
            cmd = command_queue.get_nowait()
        except queue.Empty:
            return
        if cmd.name in _ASYNC_COMMANDS:
            threading.Thread(target=_complete_command, args=(cmd,), daemon=True).start()
        else:
            _complete_command(cmd)


def _complete_command(cmd: "bot.Command"):
    """Run one command's handler, then unblock the waiting bot thread no matter what."""
    try:
        cmd.result = _run_command(cmd)
    except Exception as e:
        cmd.result = f"⚠ Error processing `{cmd.name}`: {e}"
    finally:
        cmd.done.set()


def _run_command(cmd: "bot.Command") -> str:
    global _paused
    name, args = cmd.name, cmd.args

    if name in ("whitelist", "blacklist"):
        action = args[0].lower() if args else "list"
        target = args[1] if len(args) > 1 else None
        return residents.modify_list(name, action, target)
    if name == "timeout":
        return _timeout_text(args)
    if name == "status":
        return _status_text()
    if name == "check":
        return _check_player_text(args)
    if name == "reload":
        return _do_reload(args)
    if name == "pause":
        _paused = True
        return "⏸ Pings paused — detection continues, alerts are silenced. `/resume` to re-enable."
    if name == "resume":
        _paused = False
        return "▶ Pings resumed."
    return f"Unknown command `{name}`."


def _timeout_text(args: list[str]) -> str:
    if not args:
        return "Usage: `timeout <name> [minutes]` (default 5, 0 to clear)."
    minutes = config.TIMEOUT_DEFAULT_MINUTES
    if len(args) > 1:
        try:
            minutes = int(float(args[1]))
        except ValueError:
            return f"`{args[1]}` is not a number of minutes."
    return residents.add_timeout(args[0], minutes)


def _status_text() -> str:
    s = logkit._stats
    uptime = logkit.format_duration(int(time.time() - _start_time))
    cache_warm = f"yes ({len(get_open_towns())} towns)" if get_open_towns() else "no (still building)"
    claims = claims_count()
    boundaries = f"yes ({claims} areas)" if claims else "no (still building — API fallback)"
    api = "DOWN" if _api_was_down else "up"
    paused = "  ⏸ **PAUSED**" if _paused else ""
    timeouts = residents.get_timeouts()
    return (
        f"**Wildness Monitor status**{paused}\n"
        f"Uptime: **{uptime}** · API: **{api}**\n"
        f"Georgian residents visible (last cycle): **{_last_online}**\n"
        f"Boundary index warm: **{boundaries}**\n"
        f"Open-towns cache warm: **{cache_warm}**\n"
        f"Errors (total): **{s['errors_total']}** · pings: **{s['pings_total']}**\n"
        f"Whitelist: **{len(residents.get_whitelist())}** · "
        f"Blacklist: **{len(residents.get_blacklist())}** · "
        f"Timeouts active: **{len(timeouts)}**"
    )


def _check_player_text(args: list[str]) -> str:
    if not args:
        return "Usage: `/check <name>`"
    pname = args[0]
    # Runs on an offloaded thread, so use a dedicated session (find_player creates
    # one when none is passed) rather than sharing the main loop's _session.
    result = find_player(pname)
    if result is None:
        return f"`{pname}` is not online / not visible on the map."
    if not result["is_wilderness"]:
        return f"`{pname}` is in a **town** at ({result['x']}, {result['z']})."
    towns = find_closest_open_towns(result["x"], result["z"])
    town_str = (
        " Nearest open towns: "
        + ", ".join(f"**{t['name']}** ({t['distance']}, {t['direction']})" for t in towns)
        + "."
    ) if towns else ""
    newp = " ⚠ [NEW PLAYER]" if result["is_new_player"] else ""
    return f"🌲 `{pname}`{newp} is in **wilderness** at ({result['x']}, {result['z']}).{town_str}"


def _rebuild_towns_bg():
    try:
        # Fresh session — this runs off-thread and may overlap open_towns_refresh_loop.
        towns = refresh_open_towns_cache(requests.Session())
        logkit.log(f"Open towns cache rebuilt on demand: {len(towns)} towns.")
    except Exception as e:
        logkit.log_error(f"ERROR rebuilding open towns cache (on demand): {e}")


def _do_reload(args: list[str]) -> str:
    target = args[0].lower() if args else "all"
    if target not in ("all", "residents", "towns"):
        return "Usage: `/reload [residents|towns|all]`"
    msgs = []
    if target in ("all", "residents"):
        try:
            new = residents.fetch_georgia_residents()
            residents.set_residents(new)
            residents.save_residents(new)
            msgs.append(f"residents refreshed ({len(new)})")
        except Exception as e:
            msgs.append(f"residents refresh failed: {e}")
    if target in ("all", "towns"):
        # The towns rebuild takes ~2 min and would stall the ping loop — run it
        # off-thread (it only ever rebinds the cache atomically, so this is safe).
        threading.Thread(target=_rebuild_towns_bg, daemon=True).start()
        msgs.append("open-towns rebuild started (~2 min)")
    return "🔄 " + "; ".join(msgs)


def _report_config():
    """Print, at startup, which features are ON/OFF based on env vars (loaded from
    .env). Makes a missing secret obvious in the terminal instead of silently
    disabling a feature. ASCII only, so it's readable on a Windows console too.
    """
    broadcast_on = bool(config.BROADCAST_TOKEN) and config.BROADCAST_ENABLED
    features = [
        ("Discord alerts",        bool(config.DISCORD_WEBHOOK_URL), "DISCORD_WEBHOOK_URL"),
        ("@Mercenary role ping",  bool(config.MERCENARY_ROLE_ID),   "MERCENARY_ROLE_ID"),
        ("Slash-command bot",     bool(config.DISCORD_BOT_TOKEN),   "DISCORD_BOT_TOKEN"),
        ("Broadcast hub",         broadcast_on,                     "BROADCAST_TOKEN (and BROADCAST_ENABLED=1)"),
    ]
    logkit.log("Config check (from environment / .env):")
    for label, on, var in features:
        logkit.log(f"  [{'ON ' if on else 'OFF'}] {label}" + ("" if on else f" -- set {var} to enable"))

    # Loud warning for the one that matters most: no webhook == no alerts at all.
    if not config.DISCORD_WEBHOOK_URL:
        logkit.log("  WARNING: DISCORD_WEBHOOK_URL is not set -- NO alerts will be sent to Discord.")
    # Dependent-config gotchas (the feature is on but mis-set so it won't actually work).
    if config.DISCORD_BOT_TOKEN and not (config.BOT_AUTHORIZED_USERS or config.BOT_AUTHORIZED_ROLE_ID):
        logkit.log("  WARNING: bot token set but BOT_AUTHORIZED_USERS / BOT_AUTHORIZED_ROLE_ID are empty -- "
                   "the bot will refuse every command.")
    if broadcast_on and not config.MOD_AUTHORIZED_PLAYERS:
        logkit.log("  WARNING: broadcast hub on but MOD_AUTHORIZED_PLAYERS is empty -- "
                   "mods can receive pings but cannot run /wild commands.")


def _on_stop():
    logkit.log("=== Wildness Monitor stopped ===")
    send_discord_status("🔴 **Wildness Monitor stopped.**")
    flush_webhooks(timeout=5)   # drain queued sends before the daemon worker dies


def main():
    global _api_was_down, _consecutive_failures, _api_down_since, _current_backoff

    atexit.register(_on_stop)
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (OSError, ValueError):
        pass

    logkit.log("=== Wildness Monitor started ===")
    _report_config()
    send_discord_status("🟢 **Wildness Monitor started.**")

    residents.load_filter_lists()
    logkit.log(
        f"Loaded {len(residents.get_whitelist())} whitelisted, "
        f"{len(residents.get_blacklist())} blacklisted players."
    )

    try:
        logkit.log("Fetching Georgia residents (initial)...")
        initial = residents.fetch_georgia_residents()
        residents.set_residents(initial)
        residents.save_residents(initial)
    except Exception as e:
        logkit.log(f"ERROR on initial fetch: {e}. Loading from file...")
        from_file = residents.load_residents()
        if from_file:
            residents.set_residents(from_file)
            logkit.log(f"Loaded {len(from_file)} residents from file.")
        else:
            logkit.log("No resident file found — will retry on first refresh cycle.")

    threading.Thread(target=residents.residents_refresh_loop, daemon=True).start()
    threading.Thread(target=claims_refresh_loop, daemon=True).start()
    threading.Thread(target=open_towns_refresh_loop, daemon=True).start()
    bot.start_bot(command_queue)     # no-op if no token / discord.py missing
    broadcast.start_broadcast(command_queue)   # no-op if disabled / websockets missing; queue enables /wild cmds

    while True:
        _drain_command_queue()       # apply inbound bot commands first (single-writer)
        logkit.log_summary_if_due()
        if _api_was_down:
            logkit.log(f"API down — next health check in {logkit.format_duration(_current_backoff)}.")
            # Keep draining commands while we back off, so /status / /pause stay
            # responsive (within ~2 s) even during a long outage.
            deadline = time.time() + _current_backoff
            while (remaining := deadline - time.time()) > 0:
                _drain_command_queue()
                time.sleep(min(2, remaining))
            healthy, error = check_api_health()
            if healthy:
                elapsed = int(time.time() - _api_down_since)
                duration_str = logkit.format_duration(elapsed)
                _api_was_down = False
                _api_down_since = None
                _consecutive_failures = 0
                _current_backoff = config.API_DOWN_BACKOFF_START
                logkit.log("API recovered — resuming normal monitoring.")
                send_discord_status(f"🟢 **EarthMC API is back UP** — was down for {duration_str}.")
            else:
                _current_backoff = min(_current_backoff * 2, config.API_DOWN_BACKOFF_CAP)
                logkit.log(f"Health check failed: {error}.")
        else:
            # One unified cadence: every PING_CYCLE_INTERVAL (10 s) scan all
            # monitored players and push to Discord (live-edited messages) + mod.
            try:
                ping_all_residents()
            except Exception as e:
                logkit.log_error(f"ERROR in ping cycle: {e}")
            if not _api_was_down:
                time.sleep(config.PING_CYCLE_INTERVAL)
