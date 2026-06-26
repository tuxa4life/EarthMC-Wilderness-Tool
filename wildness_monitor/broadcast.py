"""Real-time push of wilderness pings to game-client mods (optional component).

A tiny WebSocket hub. Mod clients connect, send a shared token as their first
frame, and then receive every ping as one JSON message. It runs its own asyncio
loop in a daemon thread so a slow or dead client can never stall the main ping
loop — the same principle as the async Discord webhook sender in alerts.py.

Like bot.py this is OPTIONAL: with BROADCAST_ENABLED off, no token set, or the
`websockets` package missing, start_broadcast() logs one line and returns, and
alerting is completely unaffected.

Shared-state rule: this module never writes any of the monitor's caches. For
inbound commands it follows the exact same pattern as bot.py — it only ever
.put()s a bot.Command onto the shared command_queue and lets the main loop apply
it, so the single-writer invariant holds. All socket I/O happens on the hub's own
asyncio loop; broadcast() is the one thread-safe hand-off from the main thread.
"""
import asyncio
import json
import logging
import threading

from wildness_monitor import config, bot
from wildness_monitor.logkit import log, log_error, classify_error

try:
    import websockets
except ImportError:                      # optional dependency
    websockets = None

_loop: "asyncio.AbstractEventLoop | None" = None   # the hub's event loop
_clients: set = set()                              # authenticated client sockets
_command_queue = None                              # set by start_broadcast (mod → main loop)


async def _handler(websocket):
    """One connected client. First frame must be the auth token."""
    try:
        token = await asyncio.wait_for(websocket.recv(), timeout=10)
    except Exception:
        await websocket.close(code=4001, reason="auth timeout")
        return
    if token != config.BROADCAST_TOKEN:
        await websocket.close(code=4003, reason="bad token")
        log("Broadcast client rejected (bad token).")
        return

    _clients.add(websocket)
    log(f"Broadcast client connected — {len(_clients)} online.")
    try:
        # Same socket is now bidirectional: outbound = ping broadcasts (fanned out
        # by broadcast()), inbound = /wild commands the client sends here. The loop
        # ends when the connection closes (async-for stops on ConnectionClosed).
        async for raw in websocket:
            await _handle_inbound(websocket, raw)
    except Exception:
        pass
    finally:
        _clients.discard(websocket)
        log(f"Broadcast client disconnected — {len(_clients)} online.")


def _authorized(player: str) -> bool:
    """True if this IGN may run commands. Empty allowlist = refuse everyone."""
    allow = config.MOD_AUTHORIZED_PLAYERS
    return bool(allow) and player.lower() in allow


async def _handle_inbound(websocket, raw: str):
    """Parse one inbound frame; dispatch it if it's a command, else ignore."""
    try:
        data = json.loads(raw)
    except Exception:
        return
    if isinstance(data, dict) and data.get("type") == "command":
        await _dispatch_command(websocket, data)


async def _dispatch_command(websocket, data: dict):
    """Authorize, enqueue a bot.Command, wait for the main loop, reply on the socket.

    Wire format (mod → monitor):
        {"type":"command","id":<any>,"player":"IGN","line":"whitelist add Bob"}
    Reply (monitor → mod):
        {"type":"command_result","id":<echoed>,"ok":bool,"result":"<text>"}
    `line` is everything after `/wild` — its first word is the command name.
    """
    req_id = data.get("id")
    player = str(data.get("player", "")).strip()
    line = str(data.get("line", "")).strip()

    async def reply(ok: bool, result: str):
        try:
            await websocket.send(json.dumps(
                {"type": "command_result", "id": req_id, "ok": ok, "result": result}
            ))
        except Exception:
            pass

    if _command_queue is None:
        await reply(False, "Command channel unavailable.")
        return
    if not _authorized(player):
        log(f"Mod command rejected — '{player or '?'}' not authorized: {line!r}")
        await reply(False, "You are not authorized to run wildness commands.")
        return
    parts = line.split()
    if not parts:
        await reply(False, "Empty command. Try: whitelist add <name>, pause, resume, check <name>.")
        return

    cmd = bot.Command(name=parts[0].lower(), args=parts[1:], author_id=f"mc:{player}")
    _command_queue.put(cmd)
    loop = asyncio.get_running_loop()
    applied = await loop.run_in_executor(None, cmd.wait, config.MOD_COMMAND_TIMEOUT)
    if applied and cmd.result is not None:
        await reply(True, cmd.result)
    else:
        await reply(False, "Timed out waiting for the monitor to process that command.")


async def _send_all(message: str):
    """Fan a message out to every client; drop any that error."""
    dead = []
    for ws in _clients:
        try:
            await ws.send(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


def has_clients() -> bool:
    """True if any mod client is connected — lets the caller skip building a payload
    nobody will receive. A benign cross-thread read (just an optimization hint)."""
    return bool(_clients)


def broadcast(event: dict):
    """Thread-safe: hand an event to the hub loop. Never blocks, never raises."""
    if _loop is None or not _clients:
        return
    try:
        message = json.dumps(event)
        asyncio.run_coroutine_threadsafe(_send_all(message), _loop)
    except Exception as e:
        log_error(f"broadcast failed: {classify_error(e)}")


def _run_loop():
    global _loop
    # A public port (0.0.0.0:8765) gets constant scanner/health-probe traffic that
    # fails the WebSocket handshake. The websockets library logs each one as an
    # ERROR with a full traceback — harmless but it floods journald. Silence its
    # logger; genuine hub failures still surface via our own log_error below.
    logging.getLogger("websockets").setLevel(logging.CRITICAL)

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    async def _serve():
        # ping_interval keeps idle TCP connections alive through NATs/proxies.
        async with websockets.serve(
            _handler, config.BROADCAST_HOST, config.BROADCAST_PORT,
            ping_interval=30, ping_timeout=30,
        ):
            log(f"Broadcast hub listening on "
                f"{config.BROADCAST_HOST}:{config.BROADCAST_PORT}.")
            await asyncio.Future()       # run forever

    try:
        _loop.run_until_complete(_serve())
    except Exception as e:
        log_error(f"Broadcast hub crashed: {classify_error(e)} — "
                  f"is port {config.BROADCAST_PORT} already in use?")


def start_broadcast(command_queue=None):
    """Start the hub thread. No-op (logs why) if disabled or unavailable.

    Pass command_queue to accept inbound /wild commands from the mod; without it
    the hub is broadcast-only (it will reject any command frame).
    """
    global _command_queue
    _command_queue = command_queue
    if not config.BROADCAST_ENABLED:
        log("Broadcast hub disabled (BROADCAST_ENABLED is off).")
        return
    if websockets is None:
        log("Broadcast hub disabled (`websockets` not installed).")
        return
    if not config.BROADCAST_TOKEN:
        log("Broadcast hub disabled (BROADCAST_TOKEN not set).")
        return
    if command_queue is not None and not config.MOD_AUTHORIZED_PLAYERS:
        log("WARNING: no MOD_AUTHORIZED_PLAYERS set — mod commands will be refused "
            "(broadcasts still work). Set MOD_AUTHORIZED_PLAYERS to enable /wild commands.")
    threading.Thread(target=_run_loop, daemon=True, name="broadcast").start()
