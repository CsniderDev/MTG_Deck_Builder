"""Static reference data shared across services.

The ``GAMECHANGERS`` set is the official Commander "Game Changers" list used by
the bracket system. Bracket 1 & 2 allow zero game-changers, bracket 3 allows up
to three, and brackets 4-5 are unconstrained.
"""
from __future__ import annotations

# Maximum game-changers permitted per bracket. ``None`` means unrestricted.
GAMECHANGER_LIMITS: dict[int, int | None] = {1: 0, 2: 0, 3: 3, 4: None, 5: None}


def gamechanger_limit(bracket: int) -> int | None:
    return GAMECHANGER_LIMITS.get(bracket)
