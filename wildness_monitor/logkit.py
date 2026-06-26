"""Logging, run statistics, and small formatting helpers.

Logging policy (see CLAUDE.md): routine per-cycle activity is NOT logged. Errors
log immediately via `log_error` (which bumps the error counter); every
`SUMMARY_INTERVAL` the main loop calls `log_summary_if_due` to emit one rollup
line and reset the counters.

`_stats` is a single shared dict mutated in place from the main thread only.
Import the module (`from wildness_monitor import logkit`) and mutate
`logkit._stats[...]` — never rebind it.
"""
import time
import requests

from wildness_monitor.config import SUMMARY_INTERVAL

_stats = {
    "cycles": 0,
    "pings": 0,
    "players": set(),
    "errors": 0,
    "peak_online": 0,
    # Lifetime totals (since startup) — never reset by the 6 h summary rollover.
    "errors_total": 0,
    "pings_total": 0,
}
_last_summary = time.time()


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def log_error(msg: str):
    _stats["errors"] += 1
    _stats["errors_total"] += 1
    log(msg)


def format_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def classify_error(e: Exception) -> str:
    if isinstance(e, requests.exceptions.Timeout):
        return "connection timed out"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "server unreachable"
    if isinstance(e, requests.exceptions.HTTPError):
        response = getattr(e, "response", None)
        code = response.status_code if response is not None else "?"
        if code == 429:
            return "rate limited (HTTP 429)"
        return f"HTTP {code}"
    return type(e).__name__


def log_summary_if_due():
    """Emit one summary line every SUMMARY_INTERVAL seconds, then reset counters."""
    global _last_summary
    now = time.time()
    if now - _last_summary < SUMMARY_INTERVAL:
        return
    players_str = f" ({', '.join(sorted(_stats['players']))})" if _stats["players"] else ""
    log(
        f"{format_duration(int(now - _last_summary))} summary: "
        f"{_stats['cycles']} cycles, {_stats['pings']} pings{players_str}, "
        f"{_stats['errors']} errors, peak {_stats['peak_online']} residents online."
    )
    _stats.update(cycles=0, pings=0, players=set(), errors=0, peak_online=0)
    _last_summary = now
