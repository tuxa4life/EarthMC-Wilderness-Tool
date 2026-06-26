"""Inbound Discord command bot — slash commands (`/`), run in its own thread.

The outbound webhook is send-only, so runtime control (`/whitelist`, `/status`,
`/pause`, …) needs a real **inbound** bot (`discord.py`, Gateway/websocket). To
preserve the project's single-writer invariant (all module-level caches are
written only from the main thread, no locks), this thread **never mutates shared
state**: each command parses + authorizes, drops a `Command` onto a
`queue.Queue`, blocks on an `Event` until the main ping loop has applied it and
filled in `result`, then sends the reply.

    /command → auth → command_queue.put(cmd) → cmd.done.wait() → followup

Slash-command specifics handled here:
  * Discord requires a response within 3 s, but our commands round-trip through
    the queue (up to BOT_COMMAND_TIMEOUT). So each handler `defer()`s first, then
    `followup.send()`s the result — no message_content intent is needed.
  * Commands are registered ("synced") on `on_ready`, guild-scoped when
    BOT_GUILD_ID is set (instant) or global otherwise (~1 h to propagate).

`discord.py` (2.x, for `app_commands`) is an OPTIONAL dependency, imported
lazily. If it isn't installed, or no token is configured, `start_bot` logs and
returns — the monitor keeps running with webhook-only alerting.
"""
import queue
import asyncio
import threading
from dataclasses import dataclass, field

from wildness_monitor import config
from wildness_monitor.logkit import log, log_error


@dataclass
class Command:
    """One inbound command, passed from the bot thread to the main loop.

    The main loop fills `result` and sets `done`; the bot thread waits on `done`
    and then sends `result` back to Discord.
    """
    name: str
    args: list[str]
    author_id: str
    done: threading.Event = field(default_factory=threading.Event)
    result: str | None = None

    def wait(self, timeout: float) -> bool:
        return self.done.wait(timeout)


def _authorized(user) -> bool:
    """True if the interaction's user may issue commands (ID allowlist or role)."""
    uid = str(user.id)
    if uid in config.BOT_AUTHORIZED_USERS:
        return True
    role_id = config.BOT_AUTHORIZED_ROLE_ID
    roles = getattr(user, "roles", None)   # Member has .roles; a DM User does not
    if role_id and roles:
        return any(str(r.id) == role_id for r in roles)
    return False


def start_bot(command_queue: "queue.Queue[Command]"):
    """Launch the bot in a daemon thread, if configured. Safe to always call."""
    if not config.DISCORD_BOT_TOKEN:
        log("Discord bot token not set — command bot disabled (webhook alerts still active).")
        return
    try:
        import discord  # noqa: F401
        from discord import app_commands  # noqa: F401 — 2.x; probe before spawning
    except ImportError:
        log_error(
            "discord.py (>=2.0) not installed — command bot disabled. "
            "Install it with: .venv/bin/pip install -U discord.py"
        )
        return
    if not config.BOT_AUTHORIZED_USERS and not config.BOT_AUTHORIZED_ROLE_ID:
        log("WARNING: bot has no authorized users or role — it will refuse every command.")

    threading.Thread(
        target=_run_bot, args=(command_queue,), daemon=True, name="discord-bot"
    ).start()


def _run_bot(command_queue: "queue.Queue[Command]"):
    """Thread entry point: own asyncio loop + discord client. Never returns normally."""
    import discord
    from discord import app_commands

    intents = discord.Intents.default()   # message_content NOT needed for slash commands
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    async def _dispatch(interaction, name: str, args: list[str]):
        """Common path: authorize, defer, enqueue, wait for the main loop, reply."""
        if not _authorized(interaction.user):
            log(f"Rejected unauthorized /{name} from {interaction.user} ({interaction.user.id})")
            await interaction.response.send_message(
                "⛔ You are not authorized to use this bot.", ephemeral=True
            )
            return
        chan = config.BOT_COMMAND_CHANNEL_ID
        if chan and str(interaction.channel_id) != chan:
            await interaction.response.send_message(
                f"⛔ Please use this bot in <#{chan}>.", ephemeral=True
            )
            return

        # Buys ~15 min to respond (commands round-trip through the main loop).
        await interaction.response.defer(thinking=True)
        cmd = Command(name=name, args=args, author_id=str(interaction.user.id))
        command_queue.put(cmd)
        loop = asyncio.get_running_loop()
        applied = await loop.run_in_executor(None, cmd.wait, config.BOT_COMMAND_TIMEOUT)
        text = cmd.result if (applied and cmd.result is not None) else \
            "⏳ Timed out waiting for the monitor to process that command."
        try:
            await interaction.followup.send(text[:1900])
        except Exception as e:
            log_error(f"Discord bot failed to send reply: {e}")

    # ── Command definitions ────────────────────────────────────────────────
    _list_choices = [
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
        app_commands.Choice(name="list", value="list"),
    ]

    @tree.command(name="status", description="Monitor status: uptime, online, cache, errors")
    async def _status(interaction):
        await _dispatch(interaction, "status", [])

    @tree.command(name="check", description="On-demand wilderness check for a player")
    @app_commands.describe(name="Player name to check")
    async def _check(interaction, name: str):
        await _dispatch(interaction, "check", [name])

    @tree.command(name="whitelist", description="Never-ping list (excludes a player even if Georgian)")
    @app_commands.describe(action="add / remove / list", name="Player name (for add or remove)")
    @app_commands.choices(action=_list_choices)
    async def _whitelist(interaction, action: app_commands.Choice[str], name: str = ""):
        await _dispatch(interaction, "whitelist", [action.value] + ([name] if name else []))

    @tree.command(name="blacklist", description="Always-ping list (includes a player even if not Georgian)")
    @app_commands.describe(action="add / remove / list", name="Player name (for add or remove)")
    @app_commands.choices(action=_list_choices)
    async def _blacklist(interaction, action: app_commands.Choice[str], name: str = ""):
        await _dispatch(interaction, "blacklist", [action.value] + ([name] if name else []))

    @tree.command(name="timeout", description="Temporarily stop pinging a player (auto-expires)")
    @app_commands.describe(name="Player to mute", minutes="Minutes to mute (default 5; 0 to clear)")
    async def _timeout(interaction, name: str, minutes: int = config.TIMEOUT_DEFAULT_MINUTES):
        await _dispatch(interaction, "timeout", [name, str(minutes)])

    @tree.command(name="reload", description="Refresh residents and/or rebuild the open-towns cache")
    @app_commands.describe(target="What to reload (default: all)")
    @app_commands.choices(target=[
        app_commands.Choice(name="all", value="all"),
        app_commands.Choice(name="residents", value="residents"),
        app_commands.Choice(name="towns", value="towns"),
    ])
    async def _reload(interaction, target: app_commands.Choice[str] = None):
        await _dispatch(interaction, "reload", [target.value] if target else [])

    @tree.command(name="pause", description="Silence pings (detection keeps running)")
    async def _pause(interaction):
        await _dispatch(interaction, "pause", [])

    @tree.command(name="resume", description="Re-enable pings")
    async def _resume(interaction):
        await _dispatch(interaction, "resume", [])

    @client.event
    async def on_ready():
        try:
            gid = config.BOT_GUILD_ID
            if gid:
                guild = discord.Object(id=int(gid))
                tree.copy_global_to(guild=guild)
                synced = await tree.sync(guild=guild)
                where = f"guild {gid}"
            else:
                synced = await tree.sync()
                where = "globally (up to ~1 h to appear)"
            log(f"Discord command bot connected as {client.user} — {len(synced)} slash commands synced {where}.")
        except Exception as e:
            log_error(f"Slash command sync failed: {e}")

    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        client.run(config.DISCORD_BOT_TOKEN, log_handler=None)
    except Exception as e:
        log_error(f"Discord command bot stopped: {e}")
