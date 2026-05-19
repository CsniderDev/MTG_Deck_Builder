"""Static reference data shared across services.

The ``GAMECHANGERS`` set is the official Commander "Game Changers" list used by
the bracket system. Bracket 1 & 2 allow zero game-changers, bracket 3 allows up
to three, and brackets 4-5 are unconstrained.
"""
from __future__ import annotations

# Maximum game-changers permitted per bracket. ``None`` means unrestricted.
GAMECHANGERS: frozenset[str] = frozenset(
    {
        "Ancient Tomb",
        "Cyclonic Rift",
        "Deflecting Swat",
        "Drannith Magistrate",
        "Enlightened Tutor",
        "Esper Sentinel",
        "Fierce Guardianship",
        "Force of Will",
        "Gaea's Cradle",
        "Jeweled Lotus",
        "Mana Crypt",
        "Mana Drain",
        "Mox Diamond",
        "Mystic Remora",
        "Necropotence",
        "Opposition Agent",
        "Orcish Bowmasters",
        "Rhystic Study",
        "Smothering Tithe",
        "The One Ring",
        "Vampiric Tutor",
    }
)

GAMECHANGER_LIMITS: dict[int, int | None] = {1: 0, 2: 0, 3: 3, 4: None, 5: None}


def gamechanger_limit(bracket: int) -> int | None:
    """Return the maximum number of game-changers allowed for a bracket."""
    return GAMECHANGER_LIMITS.get(bracket)
