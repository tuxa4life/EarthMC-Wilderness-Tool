"""Pure geometric helpers — no state, no I/O."""
import math

_COMPASS = ["North", "North East", "East", "South East",
            "South", "South West", "West", "North West"]


def heading(dx: int, dz: int) -> str:
    """Compass direction of a movement delta.

    Minecraft axes: +X = East, +Z = South → bearing 0° = North, clockwise.
    """
    bearing = math.degrees(math.atan2(dx, -dz)) % 360
    return _COMPASS[round(bearing / 45) % 8]
