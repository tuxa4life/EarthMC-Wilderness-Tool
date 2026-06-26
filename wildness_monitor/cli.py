"""Developer CLI — manual lookups, not used by the running service.

    python -m wildness_monitor.cli <player_name>
    python -m wildness_monitor.cli --closest-town <x> <z>
"""
import sys
import requests

from wildness_monitor.tracking import find_player
from wildness_monitor.towns import find_closest_open_towns


def main(argv: list[str] | None = None):
    argv = argv if argv is not None else sys.argv[1:]

    if not argv:
        print("Usage: python -m wildness_monitor.cli <player_name>")
        print("       python -m wildness_monitor.cli --closest-town <x> <z>")
        sys.exit(1)

    if argv[0] == "--closest-town":
        if len(argv) < 3:
            print("Usage: python -m wildness_monitor.cli --closest-town <x> <z>")
            sys.exit(1)
        cx, cz = int(argv[1]), int(argv[2])
        towns = find_closest_open_towns(cx, cz, session=requests.Session())
        if not towns:
            print("No open towns found.")
        else:
            print("Closest open towns:")
            for t in towns:
                tag = f" [nation spawn: {t.get('nation') or t['name']}]" if t.get("is_capital") else ""
                print(f"  {t['name']} at ({t['x']}, {t['z']}) — {t['distance']} blocks ({t['direction']}){tag}")
        sys.exit(0)

    player_name = argv[0]
    result = find_player(player_name)

    if result is None:
        print(f"Player '{player_name}' is not online or not visible on the map.")
        sys.exit(1)

    if not result["is_wilderness"]:
        print(f"Player '{player_name}' is in a town — skipping ping.")
        sys.exit(0)

    coords_str = f"({result['x']}, {result['z']})"
    new_player_str = " [NEW PLAYER]" if result["is_new_player"] else ""
    print(f"Pinged: '{player_name}'{new_player_str}: {coords_str}.")


if __name__ == "__main__":
    main()
