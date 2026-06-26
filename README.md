# EarthMC Wildness Monitor tool

A bot that watches Georgia nation players on EarthMC and sends Discord alerts when they're caught in the open — giving Iberian mercenaries a heads-up to go after them.

---

## What it does

About every **10 seconds** the bot checks which Georgia residents are online. If someone is standing in **wilderness** (land that isn't protected by a town), it sends a Discord alert with:

- Their username and coordinates
- A clickable link that opens their exact spot on the EarthMC live map
- The nearest places an Iberian can teleport to — the 2 closest open towns (`/t spawn`) plus the closest nation spawn (`/n spawn`), with distance and direction
- Which way the target is moving, and how long they've been out in the open
- How many other players are nearby (and how many are fellow Georgians — a possible escort)

**New accounts** (less than 24 hours old) are silently ignored — they're not worth chasing and are probably just fresh alts.

---

## One live message per target (no channel spam)

The bot does **not** post a new message every cycle. Each time a player is out in the wilderness, the bot keeps **one message and edits it in place** as they move:

- **Enters wilderness** → a fresh alert is posted (this is the one that notifies you).
- **Still out there** → that same message quietly updates with their latest position, heading, and how long they've been exposed. No new ping, no phone buzz.
- **Stays too long** → after ~100 seconds of continuous exposure, a separate loud `⚔ @Mercenary` message goes out (once per stay).
- **Leaves or logs off** → the message gets one final edit: *"left wilderness after 6m 20s — last seen (x, z)."*

So a player who sits in the open for ten minutes is a single, always-current message — not dozens of near-identical pings. Because the message is *edited* rather than re-posted, the bot refreshes it every ~10 seconds (the same speed the in-game mod sees) without crowding the channel. The channel reads like a live hunt board.

---

## The @Mercenary ping

The bot doesn't spam the @Mercenary role. It tags the role only for targets that **stick around** in the open:

> Once a player has stayed in wilderness for **`PING_ROLE_THRESHOLD` detection cycles in a row** (default **10**, i.e. ~100 seconds of continuous exposure), the bot posts one loud `⚔ @Mercenary` message — *"PlayerName has held wilderness 1m 40s — still exposed."*

It fires **once per wilderness stay** (the counter resets when they leave), so a player who briefly clips the open and runs back to a town never pings the role — only someone genuinely caught out does. Lower `PING_ROLE_THRESHOLD` to be more trigger-happy, raise it to require longer exposure. It's a normal setting in `config.py`.

---

## What the alerts look like

**When a target first appears** (this one notifies you):
```
🟢 12:34 - `PlayerName` entered wilderness at (-1500, 3200)
SomeTown        840 blocks  North East
OtherTown      1120 blocks  West
Iberia (nation spawn)  610 blocks  South
Moving North East · in wilderness 30s
⚠ 2 player(s) within 300 blocks (1 Georgian)
```

**Same message, updated silently as they move:**
```
🟢 12:34 - `PlayerName` in wilderness at (-1480, 3260) · updated 12:35:05
… (towns / movement / nearby refreshed) …
```

**If they stay out too long** (separate loud message, once per stay):
```
⚔ @Mercenary `PlayerName` has held wilderness 1m 40s — still exposed at (-1480, 3260)
```

**Final edit when they get away or log off:**
```
⬅ 12:34 - `PlayerName` left wilderness after 4m 10s — last seen (-1480, 3260)
```

---

## How it avoids hammering the EarthMC servers

The bot runs its detection cycle every ~10 seconds, so it has to be cheap. EarthMC's servers are also slow and time out under load, so it's built to be fast and polite:

- **Wilderness checks are done locally.** Instead of asking EarthMC "is this player in wilderness?" on every check (a slow call that often times out), the bot downloads every town's claim outline once and answers the question itself in microseconds — which is what makes a 10-second cycle affordable. These town boundaries are refreshed **every hour**. (The API is only used as a fallback for the first few seconds at startup, before the boundary map is built.)
- It only re-checks a player's wilderness status when they've actually **moved** since the last cycle.
- The list of towns an Iberian can teleport to is cached and refreshed once a day — finding the nearest one to a target is done entirely from memory, no network call.
- If the EarthMC API goes down, the bot backs off gracefully (1 min → 2 min → up to 10 min) and posts a Discord status message, then another when it recovers.

---

## Configuration

Secrets and IDs are **not** stored in the code — they live in a `.env` file (which is gitignored) at the project root. To set up:

1. Copy `.env.example` to `.env`.
2. Fill in your Discord webhook URL, and optionally the command-bot token, role/guild IDs, and broadcast settings.

Any value you leave blank simply disables that feature (no webhook = no Discord alerts, no bot token = no slash-command bot, and so on). The bot loads `.env` automatically — on the server, systemd injects the same file.

---

## Controlling who gets pinged

By default every Georgia resident caught in wilderness is pinged. Two name lists let you override that:

- **Whitelist** (`data/whitelist.txt`) — *never* ping these players, even if they're Georgian.
- **Blacklist** (`data/blacklist.txt`) — *always* ping these players, even if they're **not** Georgian (handy for watching a rival).

One name per line. If a name is on both lists, the whitelist wins. Edit the files by hand (picked up on the next restart) or change them live with the slash commands below.

There's also `/timeout`, a **temporary** whitelist — mute one player for a set number of minutes (default 5), after which they automatically come back. It's in-memory only, so a restart clears all timeouts.

---

## Controlling it while it runs (optional command bot)

If you set a `DISCORD_BOT_TOKEN`, the monitor also runs a small Discord bot you can drive with slash commands (only users/roles you authorize in `.env` can use them). See `DISCORD_BOT_SETUP.md` for the one-time setup.

| Command | What it does |
|---------|--------------|
| `/whitelist add\|remove\|list` | Edit the never-ping list |
| `/blacklist add\|remove\|list` | Edit the always-ping list |
| `/timeout name minutes` | Temporarily mute a player (0 clears it) |
| `/status` | Uptime, who's online, cache state, error/ping counts |
| `/check name` | One-off wilderness check for a specific player |
| `/reload all\|residents\|towns` | Re-fetch the resident list and/or rebuild the town cache |
| `/pause` · `/resume` | Silence or re-enable alerts (detection keeps running) |

Without a bot token, the monitor runs exactly the same — it just sends alerts and takes no commands.

---

## In-game broadcast (optional)

The monitor can also push every ping to a companion game-client mod over a WebSocket, so wilderness targets show up live in-game with clickable teleport suggestions. It's off unless you set a `BROADCAST_TOKEN`, and never affects the Discord alerts.

---

## Files

| File | What it is |
|------|-----------|
| `run.py` | Entry point — starts the monitor service |
| `wildness_monitor/` | The bot package (see the module map in `CLAUDE.md`) |
| `.env` / `.env.example` | Your secrets & config (gitignored) and the template to copy from |
| `data/georgia_residents.txt` | Saved list of Georgia residents (auto-updated every hour) |
| `data/whitelist.txt` / `data/blacklist.txt` | Never-ping / always-ping name lists |
| `HOSTING_GUIDE.md` | How to deploy this on a server |

---

## Running it

```powershell
.venv\Scripts\python.exe run.py
```

On startup it fetches the Georgia resident list, builds the local town-boundary map (used for wilderness detection), and kicks off the open-towns cache build in the background (about 2 minutes). Alerts start flowing as soon as the main loop is running — the teleport-town lines appear in alerts once that cache is ready.

To look up the nearest open town for a specific coordinate manually:
```powershell
.venv\Scripts\python.exe -m wildness_monitor.cli --closest-town 1000 -500
```

To check if a specific player is currently online and in wilderness:
```powershell
.venv\Scripts\python.exe -m wildness_monitor.cli PlayerName
```

---

## Tunable settings

The behavioral numbers live in `wildness_monitor/config.py`; secrets/IDs live in `.env`.

| Setting | Default | What it controls |
|---------|---------|-----------------|
| `PING_CYCLE_INTERVAL` | 10s | How often every player is checked, and live alerts + broadcasts refresh |
| `PING_ROLE_THRESHOLD` | 10 | Consecutive in-wilderness cycles (~10s each) before the @Mercenary ping fires |
| `TIMEOUT_DEFAULT_MINUTES` | 5 | Default length of a `/timeout` mute when no duration is given |
| `CLAIMS_REFRESH_INTERVAL` | 1 hour | How often the local town-boundary map is rebuilt |
| `RESIDENTS_REFRESH_INTERVAL` | 1 hour | How often the Georgia member list is re-synced |
#   E a r t h M C - W i l d e r n e s s - T o o l  
 