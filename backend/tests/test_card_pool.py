"""Tests for the candidate card pool: gathering, validation, rendering and assembly."""
from datetime import date

import pytest

from app.services.card_pool import (
    CardPool,
    assemble_deck,
    build_card_pool,
    clamp_targets,
    render_pool,
    sanitize_theme_queries,
)

ATRAXA = {"name": "Atraxa, Praetors' Voice", "color_identity": ["W", "U", "B", "G"]}


def _card(name, type_line="Sorcery", identity=(), legal=True, rank=100, cost="{1}", oracle="Does a thing.", produced=None):
    return {
        "id": f"id-{name}",
        "name": name,
        "type_line": type_line,
        "mana_cost": cost,
        "oracle_text": oracle,
        "color_identity": list(identity),
        "legalities": {"commander": "legal" if legal else "banned"},
        "edhrec_rank": rank,
        "produced_mana": produced or [],
    }


class _PoolScryfall:
    """Answers searches by substring of the query; records every query."""

    def __init__(self, responses, collection=None):
        self.responses = responses
        self.collection = collection or {}
        self.queries = []

    async def search(self, query, order="edhrec", direction="auto", limit=175):
        self.queries.append((query, order, direction))
        for needle, cards in self.responses.items():
            if "new:card" in query and "new:card" not in needle:
                continue  # newest-first searches only answer to an explicit "new:card" needle
            if needle in query:
                return cards[:limit]
        return []

    async def get_collection(self, names):
        return [self.collection[n] for n in names if n in self.collection]


# --- theme query sanitizing ------------------------------------------------------------


def test_sanitize_theme_queries_drops_scope_changing_and_malformed_queries():
    queries = [
        "o:proliferate",
        "o:proliferate",  # duplicate
        "t:goblin id<=wubrg",  # tries to widen the color identity
        "o:draw f:vintage",  # tries to change format
        'o:"unclosed',  # unbalanced quote
        "(t:elf",  # unbalanced paren
        "  kw:flying   t:creature  ",
        "otag:sacrifice-outlet",
        "o:extra",  # beyond the 4-query cap
    ]
    assert sanitize_theme_queries(queries) == [
        "o:proliferate",
        "kw:flying t:creature",
        "otag:sacrifice-outlet",
        "o:extra",
    ]
    assert sanitize_theme_queries("o:proliferate") == []  # not a list
    assert sanitize_theme_queries(["x" * 500])[0] == "x" * 120  # length cap


# --- targets -----------------------------------------------------------------------------


def test_clamp_targets_bounds_values_and_fills_theme():
    targets = clamp_targets({"lands": 60, "ramp": 99, "draw": "ten", "removal": -3, "wipe": 2}, 99)
    assert targets["lands"] == 40
    assert targets["removal"] == 0
    assert targets["draw"] == 10  # default for a non-number
    assert targets["theme"] >= 15
    assert sum(targets[k] for k in ("ramp", "draw", "removal", "wipe", "theme")) == 99 - 40


def test_clamp_targets_defaults_for_missing_plan():
    targets = clamp_targets(None, 98)
    assert targets["lands"] == 36
    assert sum(targets[k] for k in ("ramp", "draw", "removal", "wipe", "theme")) == 98 - 36


# --- building the pool --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_card_pool_validates_and_tags_candidates():
    scryfall = _PoolScryfall(
        {
            "otag:ramp": [_card("Arcane Signet", "Artifact", produced=["W"]), _card("Off Color Rock", "Artifact", ["R"])],
            "otag:removal": [_card("Illegal Removal", legal=False), _card("Swords to Plowshares", identity=["W"])],
            "new:card": [_card("Shiny New Card", identity=["G"], rank=None)],
            "o:proliferate": [_card("Evolution Sage", "Creature", ["G"]), _card("Doubling Season", "Enchantment", ["G"])],
            "t:land -t:basic": [_card("Command Tower", "Land", produced=["W", "U", "B", "G"]), _card("Forest", "Basic Land - Forest")],
        },
        collection={
            "Hidden Gem": _card("Hidden Gem", identity=["B"], rank=9000),
            "Atraxa's Staple": _card("Atraxa's Staple", identity=["U"]),
        },
    )
    recommendations = {
        "Top Cards": [
            {"name": "Atraxa's Staple", "num_decks": 9000, "potential_decks": 10000, "synergy": 0.1},
            {"name": "Hidden Gem", "num_decks": 300, "potential_decks": 10000, "synergy": 0.4},
        ]
    }
    pool = await build_card_pool(
        scryfall,
        [ATRAXA],
        recommendations,
        ["o:proliferate"],
        banned={"swords to plowshares"},
        gamechangers={"doubling season"},
        gamechanger_limit=3,
        today=date(2026, 11, 17),
    )
    names = {c.name for c in pool.cards}
    assert {"Arcane Signet", "Evolution Sage", "Doubling Season", "Command Tower", "Shiny New Card", "Hidden Gem", "Atraxa's Staple"} <= names
    assert not {"Off Color Rock", "Illegal Removal", "Swords to Plowshares", "Forest"} & names
    assert pool.rejected == {"off-color": 1, "not legal": 1, "banned list": 1, "basic": 1}
    assert "gem" in pool.get("Hidden Gem").tags
    assert "gem" not in pool.get("Atraxa's Staple").tags
    assert "new" in pool.get("Shiny New Card").tags
    assert pool.get("Doubling Season").is_game_changer
    assert pool.get("Command Tower").roles == {"land"}
    # EDHREC cards come first so they get the lowest numbers.
    assert pool.get(1).name == "Atraxa's Staple"
    # Every search is limited to the deck's identity and Commander legality.
    assert all("id<=wubg f:commander" in q for q, _, _ in scryfall.queries)
    assert any("new:card date>=2025-05-18" in q and order == "released" for q, order, _ in scryfall.queries)


@pytest.mark.asyncio
async def test_bracket_without_game_changers_excludes_them_from_the_pool():
    scryfall = _PoolScryfall({"otag:ramp": [_card("Mana Vault", "Artifact"), _card("Mind Stone", "Artifact")]})
    pool = await build_card_pool(scryfall, [ATRAXA], {}, [], set(), {"mana vault"}, 0)
    assert {c.name for c in pool.cards} == {"Mind Stone"}


@pytest.mark.asyncio
async def test_failed_search_does_not_break_the_pool():
    from app.services.scryfall import ScryfallError

    class _Flaky(_PoolScryfall):
        async def search(self, query, **kwargs):
            if "otag:removal" in query:
                raise ScryfallError("Scryfall 400: bad query")
            return await super().search(query, **kwargs)

    pool = await build_card_pool(_Flaky({"otag:ramp": [_card("Mind Stone", "Artifact")]}), [ATRAXA], {}, [], set(), set(), 3)
    assert {c.name for c in pool.cards} == {"Mind Stone"}


# --- rendering ----------------------------------------------------------------------------


def test_render_pool_shows_rules_text_only_for_unfamiliar_cards():
    pool = CardPool()
    pool.add(_card("Sol Ring", "Artifact", rank=1, oracle="Add {C}{C}."), role="ramp")
    pool.add(_card("Brand New Thing", rank=None, oracle="Proliferate twice."), role="theme", tag="new")
    text = render_pool(pool)
    assert "## Ramp" in text and "## Theme" in text
    assert "#1 Sol Ring {1} | Artifact" in text
    assert "Add {C}{C}" not in text  # staple: the model knows it
    assert "#2 Brand New Thing {1} | Sorcery [new] :: Proliferate twice." in text


def test_pool_lookup_accepts_numbers_hash_numbers_and_names():
    pool = CardPool()
    pool.add(_card("Sol Ring"))
    assert pool.get(1).name == pool.get("#1").name == pool.get("1").name == pool.get("sol ring").name == "Sol Ring"
    assert pool.get(2) is None and pool.get(0) is None and pool.get(True) is None


# --- assembly -------------------------------------------------------------------------------


def _assembly_pool():
    pool = CardPool()
    for i in range(30):
        pool.add(_card(f"Theme {i}", rank=1000 + i), role="theme")
    for role in ("ramp", "draw", "removal", "wipe"):
        for i in range(12):
            pool.add(_card(f"{role} {i}", rank=200 + i), role=role)
    for i in range(10):
        pool.add(_card(f"Dual {i}", "Land", rank=50 + i, produced=["W", "U"]), role="land")
    pool.add(_card("Game Changer A"), role="theme", tag="game changer")
    pool.add(_card("Game Changer B"), role="theme", tag="game changer")
    return pool


TARGETS = {"lands": 36, "ramp": 10, "draw": 10, "removal": 8, "wipe": 3, "theme": 32}


def test_assemble_protects_role_cards_when_model_overpicks_theme_first():
    pool = _assembly_pool()
    picks = [f"Theme {i}" for i in range(30)] + [f"ramp {i}" for i in range(10)] + [f"draw {i}" for i in range(10)]
    picks += [f"removal {i}" for i in range(8)] + [f"wipe {i}" for i in range(3)] + [f"draw {i}" for i in range(10, 12)]
    picks += [f"ramp {i}" for i in range(10, 12)] + [f"Dual {i}" for i in range(6)]
    notes = []
    deck = assemble_deck(pool, picks, TARGETS, 99, {"W", "U", "B", "G"}, 3, notes)
    names = {c.name for c in deck}
    assert sum(c.count for c in deck) == 99
    # 61 + 4 extra nonland picks for 63 slots: all role targets kept, extra role picks
    # beyond target dropped rather than theme cards or core interaction.
    assert all(f"removal {i}" in names for i in range(8))
    assert all(f"wipe {i}" in names for i in range(3))
    assert all(f"ramp {i}" in names for i in range(10))
    assert any("dropped" in n for n in notes)
    assert sum(1 for c in deck if c.name.startswith("Dual")) == 6
    assert sum(c.count for c in deck if c.category == "Land") == 36


def test_assemble_fills_short_roles_and_counts_from_the_pool():
    pool = _assembly_pool()
    notes = []
    deck = assemble_deck(pool, ["Theme 0", "Theme 1", 9999, "Not A Card"], TARGETS, 99, {"W", "U"}, 3, notes)
    names = {c.name for c in deck}
    assert sum(c.count for c in deck) == 99
    assert {"Theme 0", "Theme 1"} <= names
    for role, target in (("ramp", 10), ("draw", 10), ("removal", 8), ("wipe", 3)):
        assert sum(1 for c in deck if c.name.startswith(role)) >= target
    assert any("Ignored 2 pick(s)" in n for n in notes)
    assert any("Added" in n and "candidate pool" in n for n in notes)
    # No land picks: best-ranked nonbasics are auto-added for a 2-color deck.
    assert sum(1 for c in deck if c.name.startswith("Dual")) == 9


def test_assemble_enforces_game_changer_limit():
    pool = _assembly_pool()
    notes = []
    deck = assemble_deck(pool, ["Game Changer A", "Game Changer B"], TARGETS, 99, {"W"}, 1, notes)
    names = {c.name for c in deck}
    assert "Game Changer A" in names and "Game Changer B" not in names
    assert any("Skipped 1 game-changer" in n for n in notes)


def test_assemble_heuristic_mode_without_picks_builds_a_full_deck():
    pool = _assembly_pool()
    deck = assemble_deck(pool, [], TARGETS, 98, {"W", "U", "B", "G"}, 3, [])
    assert sum(c.count for c in deck) == 98
    basics = {c.name for c in deck if c.name in {"Plains", "Island", "Swamp", "Forest"}}
    assert basics == {"Plains", "Island", "Swamp", "Forest"}


def test_flatten_grouped_picks_puts_role_targets_then_theme_then_extras():
    from app.services.card_pool import flatten_picks

    picks = {
        "ramp": ["r1", "r2", "r3"],
        "removal": ["k1"],
        "theme": ["t1", "t2"],
        "lands": ["l1"],
        "surprise": ["s1"],
    }
    targets = {"ramp": 2, "draw": 0, "removal": 1, "wipe": 0}
    assert flatten_picks(picks, targets) == ["r1", "r2", "k1", "t1", "t2", "r3", "l1", "s1"]
    assert flatten_picks(["a", "b"], targets) == ["a", "b"]
    assert flatten_picks("nonsense", targets) == []


def test_assemble_keeps_the_models_exploration_picks_when_trimming():
    pool = _assembly_pool()
    for i in range(8):
        pool.add(_card(f"New Card {i}", rank=None), role="theme", tag="new")
    theme = [f"Theme {i}" for i in range(30)] + [f"New Card {i}" for i in range(8)]  # 38 for 32 slots
    picks = {
        "ramp": [f"ramp {i}" for i in range(10)],
        "draw": [f"draw {i}" for i in range(10)],
        "removal": [f"removal {i}" for i in range(8)],
        "wipe": [f"wipe {i}" for i in range(3)],
        "theme": theme,
    }
    deck = assemble_deck(pool, picks, TARGETS, 99, {"W", "U"}, 3, [])
    names = {c.name for c in deck}
    assert sum(c.count for c in deck) == 99
    # 6 of the model's [new] picks survive even though they were listed last.
    assert sum(1 for n in names if n.startswith("New Card")) == 6


@pytest.mark.asyncio
async def test_rarely_played_theme_cards_not_on_edhrec_are_tagged_gems():
    scryfall = _PoolScryfall(
        {
            "o:proliferate": [
                _card("Popular Theme Card", identity=["G"], rank=150),
                _card("Deep Cut", identity=["G"], rank=12000),
            ]
        }
    )
    pool = await build_card_pool(scryfall, [ATRAXA], {}, ["o:proliferate"], set(), set(), 3)
    assert "gem" in pool.get("Deep Cut").tags
    assert "gem" not in pool.get("Popular Theme Card").tags
