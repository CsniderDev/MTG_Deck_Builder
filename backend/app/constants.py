"""Static reference data shared across services.

The ``GAMECHANGERS`` set is the official Commander "Game Changers" list used by
the bracket system. Bracket 1 & 2 allow zero game-changers, bracket 3 allows up
to three, and brackets 4-5 are unconstrained.
"""
from __future__ import annotations

GAMECHANGERS: frozenset[str] = frozenset(
    {
        "Ancient Tomb",
        "Bolas's Citadel",
        "Chrome Mox",
        "Coalition Victory",
        "Demonic Tutor",
        "Drannith Magistrate",
        "Enlightened Tutor",
        "Expropriate",
        "Field of the Dead",
        "Fierce Guardianship",
        "Force of Will",
        "Gaea's Cradle",
        "Gifts Ungiven",
        "Glacial Chasm",
        "Grand Arbiter Augustin IV",
        "Grim Monolith",
        "Humility",
        "Imperial Seal",
        "Intuition",
        "Jeska's Will",
        "Jeweled Lotus",
        "Kinnan, Bonder Prodigy",
        "Mana Drain",
        "Mana Vault",
        "Mox Diamond",
        "Mystical Tutor",
        "Najeela, the Blade-Blossom",
        "Natural Order",
        "Necropotence",
        "Notion Thief",
        "Opposition Agent",
        "Orcish Bowmasters",
        "Panoptic Mirror",
        "Rhystic Study",
        "Seedborn Muse",
        "Serra's Sanctum",
        "Smothering Tithe",
        "Survival of the Fittest",
        "Sway of the Stars",
        "Tergrid, God of Fright",
        "Teferi's Protection",
        "The One Ring",
        "The Tabernacle at Pendrell Vale",
        "Thassa's Oracle",
        "Trinisphere",
        "Underworld Breach",
        "Urza, Lord High Artificer",
        "Vampiric Tutor",
        "Winota, Joiner of Forces",
        "Yuriko, the Tiger's Shadow",
    }
)

# Maximum game-changers permitted per bracket. ``None`` means unrestricted.
GAMECHANGER_LIMITS: dict[int, int | None] = {1: 0, 2: 0, 3: 3, 4: None, 5: None}


def gamechanger_limit(bracket: int) -> int | None:
    return GAMECHANGER_LIMITS.get(bracket)
