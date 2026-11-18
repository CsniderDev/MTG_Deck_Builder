"""Tests for pip-proportional basic land allocation."""
import pytest

from app.models.schemas import BuildDeckRequest, MagicCard
from app.services.deck_builder import (
    DeckBuilder,
    _allocate_proportionally,
    _balance_basic_lands,
    _color_pips,
    _deck_color_pips,
)

FIVE_COLOR_COMMANDER = {
    "name": "Test Five-Color Commander",
    "color_identity": ["W", "U", "B", "R", "G"],
    "mana_cost": "{W}{U}{B}{R}{G}",  # evenly weighted, so it doesn't skew the split
}


def _spell(name, cost, count=1):
    return MagicCard(name=name, count=count, mana_cost=cost, type_line="Instant")


def _basic(name, count):
    return MagicCard(name=name, count=count, category="Land", type_line=f"Basic Land - {name}")


# --- pip counting -----------------------------------------------------------------


def test_color_pips_counts_plain_hybrid_and_phyrexian_symbols():
    assert _color_pips("{2}{W}{W}") == {"W": 2}
    assert _color_pips("{W/U}{B}") == {"W": 0.5, "U": 0.5, "B": 1}
    assert _color_pips("{2/R}{G/P}") == {"R": 1, "G": 1}
    assert _color_pips("{3}{C}") == {}
    assert _color_pips(None) == {}


def test_deck_color_pips_skips_lands_and_weights_commander_double():
    cards = [
        _spell("Bolt", "{R}"),
        MagicCard(name="Mountain", count=10, type_line="Basic Land - Mountain", mana_cost=""),
        MagicCard(name="Odd Land", mana_cost="{G}", type_line="Land"),  # lands never count
    ]
    commander = {"mana_cost": "{W}", "color_identity": ["W", "R", "G"]}
    assert _deck_color_pips(cards, [commander], ["W", "R", "G"]) == {"W": 2, "R": 1}


def test_deck_color_pips_ignores_colors_outside_identity():
    cards = [_spell("Off-color", "{U}{U}"), _spell("On-color", "{R}")]
    assert _deck_color_pips(cards, [], ["R"]) == {"R": 1}


# --- allocation -------------------------------------------------------------------


def test_allocation_matches_requested_percentages():
    weights = {"W": 25, "U": 10, "B": 15, "R": 40, "G": 10}
    assert _allocate_proportionally(weights, 20) == {"W": 5, "U": 2, "B": 3, "R": 8, "G": 2}


def test_allocation_always_sums_to_total():
    weights = {"W": 1, "U": 1, "B": 1}
    for total in range(0, 12):
        assert sum(_allocate_proportionally(weights, total).values()) == total


def test_allocation_gives_minor_colors_at_least_one_when_possible():
    counts = _allocate_proportionally({"R": 95, "G": 5}, 10)
    assert counts == {"R": 9, "G": 1}


# --- balancing --------------------------------------------------------------------


def _five_color_deck(basics):
    # Colored pips: W 5, U 2, B 3, R 8, G 2 -> 25/10/15/40/10. Tests pair this with a
    # colorless-cost commander so the split is exactly the requested percentages.
    spells = [
        _spell("White spell", "{W}{W}{W}{W}{W}"),
        _spell("Blue spell", "{U}{U}"),
        _spell("Black spell", "{B}{B}{B}"),
        _spell("Red spell", "{R}{R}{R}{R}{R}{R}{R}{R}"),
        _spell("Green spell", "{G}{G}"),
    ]
    return spells + basics


def test_balance_redistributes_even_basics_to_match_pips():
    deck = _five_color_deck([_basic(n, 4) for n in ("Plains", "Island", "Swamp", "Mountain", "Forest")])
    colorless_commander = {"color_identity": ["W", "U", "B", "R", "G"], "mana_cost": "{5}"}
    notes = []
    added = _balance_basic_lands(deck, [colorless_commander], notes)
    counts = {c.name: c.count for c in deck if "Basic" in (c.type_line or "") or c.category == "Land"}
    assert counts == {"Plains": 5, "Island": 2, "Swamp": 3, "Mountain": 8, "Forest": 2}
    assert added == []
    assert "Balanced 20 basic lands" in notes[0]
    assert "R 40%" in notes[0]


def test_balance_adds_missing_basic_types_and_drops_unused_ones():
    deck = [_spell("Red spell", "{R}{R}{R}"), _spell("Green spell", "{G}"), _basic("Island", 8)]
    commander = {"color_identity": ["U", "R", "G"], "mana_cost": "{3}"}
    added = _balance_basic_lands(deck, [commander], [])
    counts = {c.name: c.count for c in deck if c.category == "Land"}
    # Island is in identity but no card needs blue, so it's dropped entirely.
    assert counts == {"Mountain": 6, "Forest": 2}
    assert {c.name for c in added} == {"Mountain", "Forest"}


def test_balance_leaves_deck_alone_without_colored_pips_or_basics():
    deck = [_spell("Rock", "{2}"), _basic("Plains", 5), _basic("Island", 5)]
    notes = []
    assert _balance_basic_lands(deck, [{"color_identity": ["W", "U"]}], notes) == []
    assert {c.name: c.count for c in deck if c.category == "Land"} == {"Plains": 5, "Island": 5}
    assert notes == []

    no_basics = [_spell("Bolt", "{R}"), MagicCard(name="Sacred Foundry", type_line="Land")]
    assert _balance_basic_lands(no_basics, [{"color_identity": ["R", "W"]}], notes) == []
    assert notes == []


def test_balance_does_not_touch_wastes_or_nonbasic_lands():
    deck = [
        _spell("Bolt", "{R}"),
        _basic("Wastes", 3),
        MagicCard(name="Command Tower", count=1, type_line="Land"),
        _basic("Plains", 4),
    ]
    _balance_basic_lands(deck, [{"color_identity": ["R", "W"]}], [])
    counts = {c.name: c.count for c in deck if c.name != "Bolt"}
    assert counts == {"Wastes": 3, "Command Tower": 1, "Mountain": 4}


# --- end to end through DeckBuilder ---------------------------------------------


class _ColoredScryfall:
    """Scryfall stub whose cards have real colored costs, keyed by name."""

    COSTS = {"Red Card": "{R}{R}{R}", "Green Card": "{G}"}

    async def resolve_commander(self, name):
        return {
            "id": "cmd",
            "name": "Test RG Commander",
            "type_line": "Legendary Creature",
            "legalities": {"commander": "legal"},
            "color_identity": ["R", "G"],
            "mana_cost": "{2}",
        }

    async def get_commander_companion_options(self, name):
        return {"primary_name": name, "relationship": None, "secondary_kind": None, "options": []}

    def _card(self, name):
        is_basic = name in {"Mountain", "Forest"}
        return {
            "id": f"id-{name}",
            "name": name,
            "type_line": f"Basic Land - {name}" if is_basic else "Sorcery",
            "mana_cost": "" if is_basic else self.COSTS.get(name, "{1}"),
            "legalities": {"commander": "legal"},
            "color_identity": [],
        }

    async def get_collection(self, names):
        return [self._card(n) for n in names]

    async def search(self, query, order="edhrec", direction="auto", limit=175):
        # Every search returns the test's catalog, so all of it lands in the pool.
        if "new:card" in query:
            return []
        return [self._card(n) for n in getattr(self, "catalog", [])]

    async def get_card(self, name):
        return self._card(name)

    async def aclose(self):
        pass


class _NoEdhrec:
    async def commander_page(self, name):
        return {}

    @staticmethod
    def extract_recommendations(page):
        return {}

    async def aclose(self):
        pass


class _FixedLLM:
    enabled = True
    provider_label = "Fake"
    last_failure_reason = None
    PICKS = ["Red Card", "Green Card"] + [f"Filler {i}" for i in range(61)]

    async def plan_deck(self, **kwargs):
        return {"strategy": "test", "targets": {"lands": 36}, "theme_queries": ["o:test"]}

    async def select_cards(self, **kwargs):
        return {"picks": list(self.PICKS), "explanation": "test", "notes": []}


@pytest.mark.asyncio
async def test_build_rebalances_llm_basics_to_pip_share():
    scryfall = _ColoredScryfall()
    scryfall.catalog = list(_FixedLLM.PICKS)
    builder = DeckBuilder(scryfall, _NoEdhrec(), _FixedLLM())
    response = await builder.build(BuildDeckRequest(commander="Test", bracket=3, prompt="go"))
    lands = {c.name: c.count for c in response.decklist if c.name in {"Mountain", "Forest"}}
    assert lands == {"Mountain": 27, "Forest": 9}
    assert sum(c.count for c in response.decklist) == 99
    assert any("Balanced 36 basic lands" in note for note in response.notes)



# --- trimming oversized lists -------------------------------------------------------


def test_trim_keeps_land_ratio_instead_of_cutting_only_basics():
    from app.services.deck_builder import _normalize_decklist

    # 115 cards: 81 spells + 20 nonbasic lands + 14 basics (34 lands, ~30%).
    raw = [{"name": f"Spell {i}", "type_line": "Sorcery"} for i in range(81)]
    raw += [{"name": f"Dual {i}", "type_line": "Land"} for i in range(20)]
    raw += [{"name": n, "count": c} for n, c in (("Plains", 3), ("Island", 4), ("Swamp", 3), ("Forest", 4))]
    commander = {"name": "Atraxa", "color_identity": ["W", "U", "B", "G"]}
    notes = []
    deck = _normalize_decklist(raw, commander, notes)
    lands = sum(c.count for c in deck if c.name.startswith("Dual") or c.name in {"Plains", "Island", "Swamp", "Forest"})
    assert sum(c.count for c in deck) == 99
    # 16 cuts at ~30% lands -> about 5 land cuts, so ~29 lands remain (not 24).
    assert lands == 29
    assert sum(c.count for c in deck if c.name.startswith("Dual")) == 20  # basics absorb land cuts
    assert any("Trimmed 16" in n for n in notes)


# --- counting nonbasic sources (lands, rocks, dorks) ---------------------------------


def _source(name, type_line, produced=None, oracle="", count=1):
    return MagicCard(name=name, count=count, type_line=type_line, produced_mana=produced or [], oracle_text=oracle)


def test_colored_sources_weights_lands_rocks_dorks_and_skips_non_sources():
    from app.services.deck_builder import _colored_sources

    identity = {"U", "B", "G"}
    deck = [
        _source("Breeding Pool", "Land - Forest Island", ["G", "U"]),
        _source("Command Tower", "Land", ["W", "U", "B", "R", "G"]),  # filtered to identity
        _source("Arcane Signet", "Artifact", ["W", "U", "B", "R", "G"]),  # rock: half weight
        _source("Llanowar Elves", "Creature - Elf Druid", ["G"]),  # dork: half weight
        _source("Sol Ring", "Artifact", ["C"]),  # colorless: ignored
        _source("Dark Ritual", "Instant", ["B"]),  # one-shot spell: ignored
        _source("Forest", "Basic Land - Forest", ["G"], count=5),  # basics are allocated, not counted
    ]
    assert _colored_sources(deck, identity) == {"U": 2.5, "B": 1.5, "G": 3.0}


def test_colored_sources_reads_fetch_land_targets():
    from app.services.deck_builder import _colored_sources

    identity = {"U", "B", "G"}
    deck = [
        _source("Polluted Delta", "Land", oracle="{T}, Pay 1 life, Sacrifice this land: Search your library for an Island or Swamp card, put it onto the battlefield."),
        _source("Evolving Wilds", "Land", oracle="{T}, Sacrifice this land: Search your library for a basic land card, put it onto the battlefield tapped."),
        _source("Windswept Heath", "Land", oracle="{T}, Pay 1 life, Sacrifice this land: Search your library for a Forest or Plains card, put it onto the battlefield."),
    ]
    # Delta: U+B; Wilds: any identity color; Heath: only Forest is in identity.
    assert _colored_sources(deck, identity) == {"U": 2, "B": 2, "G": 2}


def test_allocate_basics_fills_colors_left_short_by_other_sources():
    from app.services.deck_builder import _allocate_basics

    demand = {"R": 50, "G": 50}
    # Duals/rocks already give green 10 sources and red only 2: basics should go red.
    assert _allocate_basics(demand, {"R": 2, "G": 10}, 10) == {"R": 9, "G": 1}
    # With no other sources it is the plain proportional split.
    assert _allocate_basics(demand, {}, 10) == {"R": 5, "G": 5}


def test_balance_accounts_for_nonbasic_sources():
    deck = [
        _spell("Red spell", "{R}{R}{R}{R}"),
        _spell("Green spell", "{G}{G}{G}{G}"),
        *[_source(f"Green dual {i}", "Land", ["G", "U"]) for i in range(6)],
        _basic("Mountain", 5),
        _basic("Forest", 5),
    ]
    notes = []
    _balance_basic_lands(deck, [{"color_identity": ["R", "G", "U"], "mana_cost": "{3}"}], notes)
    counts = {c.name: c.count for c in deck if c.name in {"Mountain", "Forest"}}
    # Green already has 6 sources from duals, so red gets most basics: 8 Mountain +
    # 2 Forest gives 8 red and 8 green sources - exactly the 50/50 demand.
    assert counts == {"Mountain": 8, "Forest": 2}
    assert "after counting nonbasic lands, mana rocks and dorks" in notes[0]
    assert "Colored sources now: R 8, G 8" in notes[0]


# --- land-count guard ---------------------------------------------------------------


class _GuardScryfall(_ColoredScryfall):
    """Names starting with 'Land' are nonbasic lands; 'Spell' / EDHREC picks are sorceries."""

    def _card(self, name):
        card = super()._card(name)
        if name.startswith("Land") or name == "EDHREC Land":
            card["type_line"] = "Land"
            card["mana_cost"] = ""
        return card


class _GuardEdhrec:
    async def commander_page(self, name):
        return {}

    @staticmethod
    def extract_recommendations(page):
        return {
            "Lands": [{"name": "Should Not Pick (land header)", "num_decks": 99999}],
            "Top Cards": [
                {"name": "EDHREC Land", "num_decks": 5000},  # a land in a mixed group: skipped
                {"name": "Popular Spell", "num_decks": 4000},
                {"name": "Banned Spell", "num_decks": 3500},
                {"name": "Red Card", "num_decks": 3000},  # already in the deck
            ],
            "Creatures": [
                {"name": "Second Spell", "num_decks": 2000},
                {"name": "Third Spell", "num_decks": 1000},
                {"name": "Fourth Spell", "num_decks": 500},
            ],
        }

    async def aclose(self):
        pass


_RG = [{"name": "Test RG Commander", "color_identity": ["R", "G"], "mana_cost": "{2}"}]


def _typed(name, type_line, count=1, produced=None):
    return MagicCard(name=name, count=count, type_line=type_line, produced_mana=produced or [])


def _lands(deck):
    return sum(c.count for c in deck if c.type_line and "Land" in c.type_line or c.name in {"Mountain", "Forest"})


def test_modal_double_faced_spell_lands_do_not_count_as_lands():
    from app.services.deck_builder import _is_land, _land_count

    deck = [
        _typed("Sink into Stupor // Soporific Springs", "Instant // Land"),
        _typed("Dryad Arbor", "Land Creature - Forest Dryad"),
        _typed("Mountain", "Basic Land - Mountain", 33),
    ]
    assert not _is_land(deck[0])
    assert _is_land(deck[1])
    assert _land_count(deck) == 34  # 33 Mountains + Dryad Arbor; the MDFC is a spell


@pytest.mark.asyncio
async def test_too_few_lands_swaps_last_spells_for_basics():
    deck = [_typed("Red Card", "Sorcery"), _typed("Green Card", "Sorcery")]
    deck += [_typed(f"Spell {i}", "Sorcery") for i in range(67)]
    deck += [_typed(f"Land {i}", "Land") for i in range(10)] + [_typed("Mountain", "Basic Land - Mountain", 20)]
    builder = DeckBuilder(_GuardScryfall(), _NoEdhrec(), _FixedLLM())
    notes = []
    deck = await builder._enforce_land_count(deck, _RG, {}, set(), notes)

    names = {c.name for c in deck}
    assert sum(c.count for c in deck) == 99
    assert _lands(deck) == 35
    # The 5 cuts come from the end of the spell list; the first spells survive.
    assert {"Spell 62", "Spell 63", "Spell 64", "Spell 65", "Spell 66"}.isdisjoint(names)
    assert {"Red Card", "Green Card", "Spell 0"} <= names
    assert any("Deck had only 30 lands" in n and "Spell 66" in n for n in notes)


@pytest.mark.asyncio
async def test_too_many_lands_swaps_basics_for_top_edhrec_spells():
    deck = [_typed("Red Card", "Sorcery"), _typed("Green Card", "Sorcery")]
    deck += [_typed(f"Spell {i}", "Sorcery") for i in range(53)]
    deck += [_typed(f"Land {i}", "Land") for i in range(4)] + [_typed("Mountain", "Basic Land - Mountain", 40)]
    builder = DeckBuilder(_GuardScryfall(), _GuardEdhrec(), _FixedLLM())
    recommendations = _GuardEdhrec.extract_recommendations({})
    notes = []
    deck = await builder._enforce_land_count(deck, _RG, recommendations, {"banned spell"}, notes)

    names = {c.name for c in deck}
    assert sum(c.count for c in deck) == 99
    assert _lands(deck) == 40
    assert {"Popular Spell", "Second Spell", "Third Spell", "Fourth Spell"} <= names
    assert not {"Banned Spell", "EDHREC Land", "Should Not Pick (land header)"} & names
    # Basics absorb the cuts; nonbasic lands are kept.
    assert all(f"Land {i}" in names for i in range(4))
    assert any("Deck had 44 lands" in n and "Popular Spell" in n for n in notes)


@pytest.mark.asyncio
async def test_land_count_in_range_is_left_alone():
    deck = [_typed(f"Spell {i}", "Sorcery") for i in range(62)] + [_typed("Mountain", "Basic Land - Mountain", 37)]
    builder = DeckBuilder(_GuardScryfall(), _GuardEdhrec(), _FixedLLM())
    notes = []
    deck = await builder._enforce_land_count(deck, _RG, _GuardEdhrec.extract_recommendations({}), set(), notes)
    assert _lands(deck) == 37
    assert notes == []


def test_swap_skips_mana_rocks_and_dorks_unless_nothing_else():
    from app.services.deck_builder import _swap_spells_for_basics

    deck = [
        _spell("Removal", "{R}"),
        _spell("Card Draw", "{G}"),
        _source("Arcane Signet", "Artifact", ["R", "G"]),
        _source("Sol Ring", "Artifact", ["C"]),
        _basic("Mountain", 30),
    ]
    notes = []
    _swap_spells_for_basics(deck, {"R", "G"}, 2, 30, notes)
    names = {c.name for c in deck}
    assert {"Arcane Signet", "Sol Ring"} <= names  # ramp kept
    assert not {"Removal", "Card Draw"} & names  # other spells cut instead
    assert next(c for c in deck if c.name == "Mountain").count == 32

    # With only ramp left to cut, it is used as a last resort.
    _swap_spells_for_basics(deck, {"R", "G"}, 1, 32, notes)
    assert next(c for c in deck if c.name == "Mountain").count == 33
