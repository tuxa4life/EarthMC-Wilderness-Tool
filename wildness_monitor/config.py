"""All tunable constants and secrets in one place.

Secrets and deployment-specific IDs (webhook URL, bot token, broadcast token,
role/guild/user IDs, authorized players) are read from environment variables —
**never hardcoded here**. They live in the gitignored `.env` file at the project
root. In production systemd injects them via `EnvironmentFile=/opt/earthmc-monitor/.env`;
for local runs the `_load_dotenv` helper below reads the same `.env` directly.
Any value left unset disables the feature that needs it (fail-safe): no webhook →
no Discord alerts, no bot token → no command bot, no broadcast token → no hub.
"""
import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE `.env` file.

    Real environment variables always win — we only fill in keys that aren't
    already set, so systemd's EnvironmentFile (server) takes precedence and this
    is just a convenience for local `python run.py`. Silently does nothing if the
    file is missing. No third-party dependency (no python-dotenv needed).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── EarthMC endpoints ──────────────────────────────────────────────────────
PLAYERS_URL      = "https://map.earthmc.net/tiles/players.json"
LOCATION_API_URL = "https://api.earthmc.net/v4/location"
PLAYERS_API_URL  = "https://api.earthmc.net/v4/players"
TOWNS_API_URL    = "https://api.earthmc.net/v4/towns"
NATIONS_API_URL  = "https://api.earthmc.net/v4/nations"
MARKERS_URL      = "https://map.earthmc.net/tiles/minecraft_overworld/markers.json"


def map_url(x: int, z: int) -> str:
    """The live-map deep link for a coordinate (one definition, shared by the
    Discord alert link and the broadcast payload, so they can't drift apart)."""
    return f"https://map.earthmc.net/?world=minecraft_overworld&zoom=5&x={x}&z={z}"


# ── On-disk data files ─────────────────────────────────────────────────────
# All runtime text files live under data/ (relative to the working directory —
# /opt/earthmc-monitor on the server). The directory is created on first write.
DATA_DIR = "data"

# ── Nation / residents ─────────────────────────────────────────────────────
NATION_NAME = "Georgia"
RESIDENTS_FILE = os.path.join(DATA_DIR, "georgia_residents.txt")
RESIDENTS_REFRESH_INTERVAL = 1 * 3600   # re-fetch residents every 1 h

# ── Whitelist / blacklist ──────────────────────────────────────────────────
# whitelist = never ping (even if Georgian); blacklist = always ping (even if
# not Georgian). Whitelist wins when a name is on both. One name per line.
WHITELIST_FILE = os.path.join(DATA_DIR, "whitelist.txt")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.txt")

# Per-player TEMPORARY mute (/timeout). In-memory only (cleared on restart), so no
# file. Default duration when none is given; 0 minutes clears an active timeout.
TIMEOUT_DEFAULT_MINUTES = 5

# ── Open-towns cache ───────────────────────────────────────────────────────
EXCLUDED_NATIONS = {"georgia"}   # towns from these nations are excluded (lowercase)
ALLY_NATIONS = {"iberia"}        # public towns from these nations skip the canOutsidersSpawn check
OPEN_TOWNS_CACHE_TTL = 1 * 24 * 3600   # open-towns (teleport targets) cache lifetime — 1 day
# Local wilderness detection: all-town claim polygons parsed from markers.json into
# a point-in-polygon index, so "is this player in wilderness?" needs no per-check
# API call. Town borders move slowly, so a 1 h rebuild keeps it fresh and cheap.
CLAIMS_REFRESH_INTERVAL = 1 * 3600     # rebuild the town-boundary index every 1 h
TOWNS_BATCH_SIZE = 40            # towns per v4/towns request (~115 KB/batch, <1 s)
TOWNS_BATCH_DELAY = 0.3          # seconds between batches to avoid rate-limiting
TOWNS_IN_ALERT = 3               # how many nearest open towns to list per alert
NATIONS_BATCH_SIZE = 50          # nations per v4/nations request when fetching /n spawn points

# ── Movement / proximity ───────────────────────────────────────────────────
SQUAD_RADIUS = 300               # blocks — count other online players within this range
STATIONARY_MOVE_BLOCKS = 15      # blocks moved since last cycle below which no heading is shown

# ── Monitor loop ───────────────────────────────────────────────────────────
# Single detection cadence: every PING_CYCLE_INTERVAL the main loop scans ALL
# monitored players and pushes to both Discord (one live-edited message per target
# — no new message per cycle, so a fast cadence doesn't crowd the channel) and the
# broadcast/mod clients.
PING_CYCLE_INTERVAL = 10                  # seconds between detection cycles (positions, Discord, broadcast)
CONSECUTIVE_FAILURES_THRESHOLD = 3       # failures before marking API down
API_DOWN_BACKOFF_START = 60              # initial backoff seconds
API_DOWN_BACKOFF_CAP = 10 * 60           # max backoff seconds
SUMMARY_INTERVAL = 6 * 3600              # seconds between log summary lines

# ── Discord ────────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")   # "" → no Discord alerts
MERCENARY_ROLE_ID   = os.getenv("MERCENARY_ROLE_ID", "")     # "" → role never mentioned
# @Mercenary fires once a target has stayed in wilderness for this many consecutive
# detection cycles (≈ PING_ROLE_THRESHOLD × PING_CYCLE_INTERVAL seconds). At 10 s
# cycles, 10 ≈ 100 s of continuous exposure before the role is pinged.
PING_ROLE_THRESHOLD = 6

# ── Discord webhook delivery (async background sender) ─────────────────────
WEBHOOK_TIMEOUT       = 10      # seconds per POST attempt (Discord normally replies <1 s)
WEBHOOK_RETRIES       = 2       # extra attempts after the first, on timeout / 5xx / 429
WEBHOOK_RETRY_BACKOFF = 1.0     # seconds between attempts (overridden by Retry-After on 429)
WEBHOOK_QUEUE_MAX     = 1000    # max queued messages before overflow drop

# ── Discord command bot (inbound — see DISCORD_BOT_SETUP.md) ───────────────
# The bot is OPTIONAL: with no token the monitor runs exactly as before and only
# the outbound webhook is used. The token is NOT the webhook URL.
DISCORD_BOT_TOKEN  = os.getenv("DISCORD_BOT_TOKEN", "")   # "" → command bot disabled
# Authorization (mandatory in practice): commands are accepted only from a user
# whose ID is in BOT_AUTHORIZED_USERS, or who holds BOT_AUTHORIZED_ROLE_ID. With
# neither set the bot connects but refuses every command (fail-safe).
BOT_AUTHORIZED_USERS = {
    s.strip() for s in os.getenv("BOT_AUTHORIZED_USERS", "").split(",") if s.strip()
}
BOT_AUTHORIZED_ROLE_ID = os.getenv("BOT_AUTHORIZED_ROLE_ID", "")   # "" = role check disabled
BOT_COMMAND_CHANNEL_ID = os.getenv("BOT_COMMAND_CHANNEL_ID", "")   # "" = any channel
BOT_COMMAND_TIMEOUT    = 40     # seconds the bot waits for the main loop to apply a command
# Slash commands are registered ("synced") to this one server for INSTANT
# availability. Right-click the server icon → Copy Server ID. "" → global sync
# (works everywhere the bot is, but can take up to ~1 h to appear).
BOT_GUILD_ID = os.getenv("BOT_GUILD_ID", "")

# ── Game-chat broadcast hub (optional — see Mod setup/) ────────────────────
# Pushes every wilderness ping to connected client mods over a WebSocket.
# OFF by default; flip BROADCAST_ENABLED on and set a token to turn it on.
BROADCAST_ENABLED = os.getenv("BROADCAST_ENABLED", "1") == "1"
BROADCAST_HOST    = os.getenv("BROADCAST_HOST", "0.0.0.0")   # 0.0.0.0 = all interfaces
BROADCAST_PORT    = int(os.getenv("BROADCAST_PORT", "8765"))
# Shared secret each mod must send as its first frame. MUST be set to enable the
# hub. Generate one with:  python -c "import secrets; print(secrets.token_urlsafe(24))"
BROADCAST_TOKEN   = os.getenv("BROADCAST_TOKEN", "")   # "" → hub disabled

# Inbound commands FROM the mod over that same WebSocket (/wild whitelist add …,
# /wild pause, /wild check …). Only players whose IGN is in this allowlist may run
# them; empty = nobody (clients can still RECEIVE broadcasts, they just can't issue
# commands). Comma-separated env, lowercased. NOTE: the IGN is self-reported by the
# client, so this is a convenience gate on top of BROADCAST_TOKEN, not a hard
# identity check — anyone with the token could spoof an IGN. Keep the token secret.
MOD_AUTHORIZED_PLAYERS = {
    p.strip().lower()
    for p in os.getenv("MOD_AUTHORIZED_PLAYERS", "").split(",")
    if p.strip()
}
MOD_COMMAND_TIMEOUT = int(os.getenv("MOD_COMMAND_TIMEOUT", "40"))  # s to wait for the main loop
