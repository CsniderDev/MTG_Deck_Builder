import pytest

from app.models.schemas import BuildDeckRequest, MagicCard, RevampDeckRequest
from app.services.deck_builder import (
    DECK_SIZE,
    DeckBuilder,
    DeckBuilderError,
    _enforce_gamechanger_limit,
    _heuristic_decklist,
    _normalize_decklist,
)


ATRAXA = {
    "id": "abc-123",
    "name": "Atraxa, Praetors' Voice",
    "type_line": "Legendary Creature - Phyrexian Angel Horror",
    "oracle_text": "Flying, vigilance, deathtouch, lifelink",
    "legalities": {"commander": "legal"},
    "color_identity": ["W", "U", "B", "G"],
    "colors": ["W", "U", "B", "G"],
    "mana_cost": "{G}{W}{U}{B}",
    "image_uris": {"normal": "https://img.test/atraxa-normal.jpg"},
}

MONOR = {
    "id": "def-456",
    "name": "Krenko, Mob Boss",
    "type_line": "Legendary Creature - Goblin Warrior",
    "oracle_text": "Tap: Create X 1/1 red Goblin tokens.",
    "legalities": {"commander": "legal"},
    "color_identity": ["R"],
    "image_uris": {"normal": "https://img.test/krenko-normal.jpg"},
}

TYMNA = {
    "id": "tymna-1",
    "name": "Tymna the Weaver",
    "type_line": "Legendary Creature - Human Cleric",
    "oracle_text": "Lifelink\nAt the beginning of your postcombat main phase...\nPartner (You can have two commanders if both have partner.)",
    "legalities": {"commander": "legal"},
    "color_identity": ["W", "B"],
    "image_uris": {"normal": "https://img.test/tymna.jpg"},
}

THRASIOS = {
    "id": "thrasios-1",
    "name": "Thrasios, Triton Hero",
    "type_line": "Legendary Creature - Merfolk Wizard",
    "oracle_text": "Partner (You can have two commanders if both have partner.)",
    "legalities": {"commander": "legal"},
    "color_identity": ["G", "U"],
    "image_uris": {"normal": "https://img.test/thrasios.jpg"},
}


def test_normalize_pads_to_99_with_basics_for_atraxa():
    raw = [{"name": "Sol Ring", "count": 1, "category": "Ramp"}]
    notes: list[str] = []
    deck = _normalize_decklist(raw, ATRAXA, notes)
    total = sum(c.count for c in deck)
    assert total == DECK_SIZE
    basic_names = {"Plains", "Island", "Swamp", "Forest"}
    found_basics = {c.name for c in deck if c.name in basic_names}
    # All four mono-color basics should appear in WUBG identity.
    assert found_basics == basic_names
    assert any("Padded" in n for n in notes)


def test_normalize_pads_with_wastes_for_colorless():
    colorless = dict(MONOR, name="Karn, Silver Golem", color_identity=[])
    deck = _normalize_decklist([{"name": "Sol Ring"}], colorless, [])
    assert sum(c.count for c in deck) == DECK_SIZE
    assert any(c.name == "Wastes" for c in deck)


def test_normalize_trims_when_over_99():
    raw = [{"name": f"Card {i}"} for i in range(120)]
    notes: list[str] = []
    deck = _normalize_decklist(raw, ATRAXA, notes)
    assert sum(c.count for c in deck) == DECK_SIZE
    assert any("Trimmed" in n for n in notes)


def test_normalize_enforces_singleton_for_nonbasics():
    raw = [{"name": "Sol Ring", "count": 4}, {"name": "Sol Ring", "count": 2}]
    deck = _normalize_decklist(raw, ATRAXA, [])
    sols = [c for c in deck if c.name == "Sol Ring"]
    assert len(sols) == 1 and sols[0].count == 1


def test_normalize_merges_basic_duplicates():
    raw = [{"name": "Island", "count": 5}, {"name": "Island", "count": 3}, {"name": "Sol Ring"}]
    deck = _normalize_decklist(raw, ATRAXA, [])
    islands = [c for c in deck if c.name == "Island"]
    assert len(islands) == 1 and islands[0].count >= 8


def test_normalize_drops_commander_from_list():
    raw = [{"name": "Atraxa, Praetors' Voice"}, {"name": "Sol Ring"}]
    deck = _normalize_decklist(raw, ATRAXA, [])
    assert all(c.name != "Atraxa, Praetors' Voice" for c in deck)


# --- Integration-style tests with stubs ----------------------------------


class _StubScryfall:
    """Scryfall stub. ``search`` serves a catalog of generic legal cards ("Card 0".."Card 79")."""

    def __init__(self):
        self.collection_calls = 0
        self.collection_requests: list[list[str]] = []
        self.searches: list[str] = []

    async def search(self, query, order="edhrec", direction="auto", limit=175):
        self.searches.append(query)
        if "new:card" in query:
            return []
        return (await self.get_collection([f"Card {i}" for i in range(80)], count=False))[:limit]

    async def resolve_commander(self, name):
        if "tymna" in name.lower():
            return TYMNA
        return ATRAXA

    async def get_card(self, name):
        lowered = name.lower()
        if "thrasios" in lowered:
            return THRASIOS
        if "cloudsea djinn" in lowered:
            return {
                "id": "nyxbloom-1",
                "name": "Nyxbloom Ancient",
                "printed_name": "The Cloudsea Djinn",
                "type_line": "Creature - Elemental",
                "oracle_text": "If you tap a permanent for mana, it produces three times as much of that mana instead.",
                "legalities": {"commander": "legal"},
                "color_identity": ["G"],
                "image_uris": {"normal": "https://img.test/nyxbloom.jpg"},
            }
        return {
            "id": f"id-{lowered.replace(' ', '-')}",
            "name": name,
            "type_line": "Artifact",
            "oracle_text": f"Rules text for {name}",
            "mana_cost": "{1}",
            "colors": [],
            "color_identity": [],
            "legalities": {"commander": "legal"},
            "image_uris": {"normal": f"https://img.test/{lowered.replace(' ', '-')}.jpg"},
        }

    async def get_commander_companion_options(self, name):
        if "tymna" in name.lower():
            return {
                "primary_name": "Tymna the Weaver",
                "relationship": "Partner",
                "secondary_kind": "commander",
                "options": ["Thrasios, Triton Hero"],
            }
        return {
            "primary_name": name,
            "relationship": None,
            "secondary_kind": None,
            "options": [],
        }

    async def get_collection(self, names, count=True):
        if count:
            self.collection_calls += 1
            self.collection_requests.append(list(names))
        return [
            {
                "id": f"id-{name.lower().replace(' ', '-')}",
                "name": name,
                "type_line": "Artifact",
                "oracle_text": f"Rules text for {name}",
                "mana_cost": "{1}",
                "colors": [],
                "color_identity": [],
                "legalities": {"commander": "legal"},
                "image_uris": {"normal": f"https://img.test/{name.lower().replace(' ', '-')}.jpg"},
            }
            for name in names
            if "cloudsea djinn" not in name.lower()
        ]

    async def aclose(self):
        pass


class _StubEDHRec:
    async def commander_page(self, name):
        return {"container": {"json_dict": {"cardlists": []}}}

    @staticmethod
    def extract_recommendations(page):
        return {
            "Top Cards": [
                {"name": "Sol Ring"},
                {"name": "Arcane Signet"},
                {"name": "Command Tower"},
            ]
        }

    async def aclose(self):
        pass


class _StubLLM:
    """LLM stub: plan/select answer from ``payload`` (picks are the payload's card names)."""

    provider_label = "Stub"
    last_failure_reason = None

    def __init__(self, payload=None, enabled=True):
        self._payload = payload
        self.enabled = enabled

    async def plan_deck(self, **kwargs):
        if self._payload is None:
            return None
        return {"strategy": "Stub plan.", "targets": {}, "theme_queries": ["o:test"]}

    async def select_cards(self, **kwargs):
        if self._payload is None:
            return None
        return {
            "picks": [entry["name"] for entry in self._payload.get("decklist", [])],
            "explanation": self._payload.get("explanation"),
            "notes": self._payload.get("notes", []),
        }

    async def revamp_deck(self, **kwargs):
        return self._payload


@pytest.mark.asyncio
async def test_build_with_llm_returns_llm_source():
    payload = {
        "decklist": [{"name": f"Card {i}", "count": 1, "category": "Misc"} for i in range(60)]
        + [{"name": "Sol Ring", "count": 1, "category": "Ramp"}],
        "explanation": "Test plan.",
        "notes": ["Test note."],
        "substitutions": [],
    }
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload))
    result = await builder.build(BuildDeckRequest(commander="Atraxa", bracket=3, prompt="combo"))
    assert result.source == "llm"
    assert result.version == 1
    assert result.bracket == 3
    assert sum(c.count for c in result.decklist) == DECK_SIZE
    assert result.explanation == "Test plan."
    assert "Test note." in result.notes
    assert result.substitutions == []


@pytest.mark.asyncio
async def test_build_without_llm_uses_heuristic():
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload=None, enabled=False))
    result = await builder.build(BuildDeckRequest(commander="Atraxa", bracket=2, prompt=""))
    assert result.source == "heuristic"
    assert sum(c.count for c in result.decklist) == DECK_SIZE
    names = {c.name for c in result.decklist}
    assert "Sol Ring" in names or "Command Tower" in names
    sol_ring = next(card for card in result.decklist if card.name == "Sol Ring")
    assert sol_ring.image_uris is not None
    assert sol_ring.image_uris.normal == "https://img.test/sol-ring.jpg"


@pytest.mark.asyncio
async def test_build_hydrates_from_pool_data_without_refetching_picked_cards():
    payload = {
        "decklist": [{"name": f"Card {i}", "count": 1, "category": "Misc"} for i in range(63)],
        "explanation": "Fast plan.",
    }
    scryfall = _StubScryfall()
    builder = DeckBuilder(scryfall, _StubEDHRec(), _StubLLM(payload))
    result = await builder.build(BuildDeckRequest(commander="Atraxa", bracket=3, prompt="combo"))
    assert result.source == "llm"
    assert sum(c.count for c in result.decklist) == DECK_SIZE
    # The first lookup is the EDHREC names for the pool; after that only basics (which
    # are not pool cards) may be fetched - never the picked cards again.
    later_requests = {name for request in scryfall.collection_requests[1:] for name in request}
    assert later_requests <= {"Plains", "Island", "Swamp", "Forest", "Mountain", "Wastes"}


@pytest.mark.asyncio
async def test_build_accepts_valid_secondary_commander_and_unions_color_identity():
    payload = {
        "decklist": [{"name": "Growth Spiral", "count": 1, "category": "Draw"}],
        "explanation": "Partner plan.",
        "substitutions": [],
    }
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload))
    result = await builder.build(
        BuildDeckRequest(
            commander="Tymna the Weaver",
            secondary_commander="Thrasios, Triton Hero",
            bracket=3,
            prompt="value",
        )
    )
    assert result.secondary_commander is not None
    assert result.secondary_commander.name == "Thrasios, Triton Hero"


@pytest.mark.asyncio
async def test_validate_decklist_accepts_alternate_printed_name_via_fuzzy_lookup():
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM())
    valid_cards, invalid_cards = await builder.validate_decklist([{"name": "The Cloudsea Djinn"}], ["G"])
    assert invalid_cards == []
    assert "the cloudsea djinn" in valid_cards
    assert valid_cards["the cloudsea djinn"]["name"] == "Nyxbloom Ancient"


@pytest.mark.asyncio
async def test_revamp_increments_version():
    payload = {
        "decklist": [{"name": "Sol Ring", "count": 1}],
        "explanation": "Revised.",
        "substitutions": [
            {
                "removed": [{"name": "Old Card", "count": 1}],
                "added": [{"name": "Sol Ring", "count": 1}],
                "explanation": "Adds faster mana to accelerate the new game plan.",
            }
        ],
    }
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload))
    prev = [MagicCard(name="Old Card", count=1)]
    request = RevampDeckRequest(
        commander="Atraxa",
        bracket=3,
        prompt="",
        previous_version=2,
        previous_decklist=prev,
        change_request="add more removal",
    )
    result = await builder.revamp(request)
    assert result.version == 3
    assert result.source == "llm"
    assert result.substitutions[0].removed[0].name == "Old Card"
    assert result.substitutions[0].added[0].name == "Sol Ring"
    assert "faster mana" in result.substitutions[0].explanation


@pytest.mark.asyncio
async def test_revamp_without_llm_falls_back_with_note():
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload=None, enabled=False))
    request = RevampDeckRequest(
        commander="Atraxa",
        bracket=3,
        prompt="",
        previous_version=1,
        previous_decklist=[MagicCard(name="X")],
        change_request="anything",
    )
    result = await builder.revamp(request)
    assert result.source == "heuristic"
    assert result.version == 1
    assert any("LLM unavailable" in n for n in result.notes)


@pytest.mark.asyncio
async def test_revamp_with_unchanged_llm_deck_does_not_increment_version():
    payload = {
        "decklist": [{"name": "Sol Ring", "count": 1}],
        "explanation": "No changes needed.",
        "substitutions": [],
    }
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload))
    request = RevampDeckRequest(
        commander="Atraxa",
        bracket=3,
        prompt="",
        previous_version=4,
        previous_decklist=[MagicCard(name="Sol Ring", count=1)],
        change_request="do basically the same thing",
    )
    result = await builder.revamp(request)
    assert result.version == 4


# --- Game-changer enforcement -------------------------------------------


@pytest.mark.asyncio
async def test_build_rejects_prompt_when_llm_disabled():
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload=None, enabled=False))
    with pytest.raises(DeckBuilderError, match="no LLM API key"):
        await builder.build(BuildDeckRequest(commander="Atraxa", bracket=3, prompt="combo"))


@pytest.mark.asyncio
async def test_build_raises_when_llm_fails_with_prompt():
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload=None, enabled=True))
    with pytest.raises(DeckBuilderError, match="failed to produce"):
        await builder.build(BuildDeckRequest(commander="Atraxa", bracket=3, prompt="combo"))


@pytest.mark.asyncio
async def test_build_without_prompt_skips_llm_even_when_enabled():
    # If the LLM stub is "enabled" but no prompt is given, we should still take the
    # heuristic path without calling it.
    class _RecordingLLM(_StubLLM):
        def __init__(self):
            super().__init__(payload=None, enabled=True)
            self.calls = 0

        async def plan_deck(self, **kwargs):
            self.calls += 1
            return None

        async def select_cards(self, **kwargs):
            self.calls += 1
            return None

    llm = _RecordingLLM()
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), llm)
    result = await builder.build(BuildDeckRequest(commander="Atraxa", bracket=2, prompt=""))
    assert llm.calls == 0
    assert result.source == "heuristic"


def test_enforce_gamechanger_limit_drops_excess_for_bracket_3():
    raw = [
        {"name": "Sol Ring"},
        {"name": "Rhystic Study"},
        {"name": "Smothering Tithe"},
        {"name": "Mana Drain"},
        {"name": "The One Ring"},
        {"name": "Arcane Signet"},
    ]
    notes: list[str] = []
    kept = _enforce_gamechanger_limit(raw, bracket=3, notes=notes)
    kept_names = [c["name"] for c in kept]
    # Sol Ring and Arcane Signet are not on the list; both should survive.
    assert "Sol Ring" in kept_names
    assert "Arcane Signet" in kept_names
    # Only the first three game-changers (in original order) should remain.
    gc_kept = [n for n in kept_names if n in {
        "Rhystic Study", "Smothering Tithe", "Mana Drain", "The One Ring"
    }]
    assert gc_kept == ["Rhystic Study", "Smothering Tithe", "Mana Drain"]
    assert any("game-changer" in n.lower() for n in notes)


def test_enforce_gamechanger_limit_drops_all_for_bracket_2():
    raw = [{"name": "Rhystic Study"}, {"name": "Sol Ring"}, {"name": "Mana Drain"}]
    notes: list[str] = []
    kept = _enforce_gamechanger_limit(raw, bracket=2, notes=notes)
    assert [c["name"] for c in kept] == ["Sol Ring"]
    assert any("game-changer" in n.lower() for n in notes)


def test_enforce_gamechanger_limit_unrestricted_for_bracket_4():
    raw = [{"name": "Rhystic Study"}, {"name": "Mana Drain"}, {"name": "The One Ring"}]
    notes: list[str] = []
    kept = _enforce_gamechanger_limit(raw, bracket=4, notes=notes)
    assert len(kept) == 3
    assert notes == []


def test_heuristic_decklist_respects_bracket_2_limit():
    recommendations = {
        "Top Cards": [
            {"name": "Rhystic Study"},
            {"name": "Smothering Tithe"},
            {"name": "Mana Drain"},
            {"name": "Sol Ring"},
            {"name": "Arcane Signet"},
            {"name": "Command Tower"},
        ]
    }
    picks = _heuristic_decklist(ATRAXA, recommendations, bracket=2)
    names = {p["name"] for p in picks}
    # Brackets 1 and 2 forbid game-changers entirely.
    assert names.isdisjoint({"Rhystic Study", "Smothering Tithe", "Mana Drain"})
    assert {"Sol Ring", "Arcane Signet", "Command Tower"}.issubset(names)


def test_heuristic_decklist_caps_bracket_3_at_three_gamechangers():
    recommendations = {
        "Top Cards": [
            {"name": "Rhystic Study"},
            {"name": "Smothering Tithe"},
            {"name": "Mana Drain"},
            {"name": "The One Ring"},
            {"name": "Necropotence"},
            {"name": "Sol Ring"},
        ]
    }
    picks = _heuristic_decklist(ATRAXA, recommendations, bracket=3)
    gc_picked = [p["name"] for p in picks if p["name"] in {
        "Rhystic Study", "Smothering Tithe", "Mana Drain", "The One Ring", "Necropotence"
    }]
    assert len(gc_picked) == 3


@pytest.mark.asyncio
async def test_revamp_drops_off_color_cards_instead_of_keeping_them():
    class _OffColorScryfall(_StubScryfall):
        async def get_collection(self, names, count=True):
            cards = await super().get_collection(names, count)
            for card in cards:
                if card["name"] == "Lightning Bolt":
                    card["color_identity"] = ["R"]
            return cards

    payload = {
        "decklist": [{"name": "Sol Ring", "count": 1}, {"name": "Lightning Bolt", "count": 1}],
        "explanation": "Revised.",
        "substitutions": [],
    }
    builder = DeckBuilder(_OffColorScryfall(), _StubEDHRec(), _StubLLM(payload))
    request = RevampDeckRequest(
        commander="Atraxa",
        bracket=3,
        previous_decklist=[MagicCard(name="Old Card", count=1)],
        change_request="add burn",
    )
    result = await builder.revamp(request)
    names = {c.name for c in result.decklist}
    assert "Sol Ring" in names
    assert "Lightning Bolt" not in names
    # The full list (no swaps) is turned into swaps; the off-color one is reverted.
    assert any("Reverted swap(s)" in note and "Lightning Bolt (Color Mismatch)" in note for note in result.notes)
    assert all(card.name != "Lightning Bolt" for sub in result.substitutions for card in sub.added)


class _OffColorBoltScryfall(_StubScryfall):
    async def get_collection(self, names, count=True):
        cards = await super().get_collection(names, count)
        for card in cards:
            if card["name"] == "Lightning Bolt":
                card["color_identity"] = ["R"]
        return cards


@pytest.mark.asyncio
async def test_revamp_reverts_swaps_that_add_off_color_cards():
    payload = {
        # The LLM swapped Old Card -> Lightning Bolt (off-color for Atraxa) and
        # Other Card -> Sol Ring (fine). Lightning Bolt is not even in its decklist.
        "decklist": [{"name": "Sol Ring", "count": 1}, {"name": "Keeper", "count": 1}],
        "explanation": "Revised.",
        "substitutions": [
            {
                "removed": [{"name": "Old Card", "count": 1}],
                "added": [{"name": "Lightning Bolt", "count": 1}],
                "explanation": "Burn.",
            },
            {
                "removed": [{"name": "Other Card", "count": 1}],
                "added": [{"name": "Sol Ring", "count": 1}],
                "explanation": "Ramp.",
            },
        ],
    }
    builder = DeckBuilder(_OffColorBoltScryfall(), _StubEDHRec(), _StubLLM(payload))
    request = RevampDeckRequest(
        commander="Atraxa",
        bracket=3,
        previous_decklist=[
            MagicCard(name="Old Card", count=1, category="Utility"),
            MagicCard(name="Other Card", count=1),
            MagicCard(name="Keeper", count=1),
        ],
        change_request="add burn and ramp",
    )
    result = await builder.revamp(request)
    names = {c.name for c in result.decklist}
    # The illegal swap is gone from the suggestions and its removed card is back.
    assert [s.added[0].name for s in result.substitutions] == ["Sol Ring"]
    assert "Old Card" in names and "Lightning Bolt" not in names
    assert "Other Card" not in names  # the valid swap still happened
    assert any("Reverted swap(s)" in n and "Old Card -> Lightning Bolt (Color Mismatch)" in n for n in result.notes)


@pytest.mark.asyncio
async def test_revamp_enforces_35_land_floor_without_cutting_just_added_cards():
    # The user's deck is already short on lands (29); one swap adds New Addition.
    previous = (
        [MagicCard(name=f"Card {i}", count=1) for i in range(69)]
        + [MagicCard(name="Card 99", count=1)]
        + [MagicCard(name="Plains", count=29)]
    )
    payload = {
        "explanation": "Revised.",
        "substitutions": [
            {"removed": [{"name": "Card 99", "count": 1}], "added": [{"name": "New Addition", "count": 1}], "explanation": "x"}
        ],
    }
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload))
    request = RevampDeckRequest(
        commander="Atraxa",
        bracket=3,
        previous_decklist=previous,
        change_request="swap one card",
    )
    result = await builder.revamp(request)
    names = {c.name for c in result.decklist}
    basics = sum(c.count for c in result.decklist if c.name in {"Plains", "Island", "Swamp", "Forest"})
    assert sum(c.count for c in result.decklist) == DECK_SIZE
    assert basics == 35
    assert "New Addition" in names  # listed last, but protected as a just-added card
    assert any("Deck had only 29 lands" in n for n in result.notes)


# --- keeping the game plan across revisions ---------------------------------------------


def test_next_plan_keeps_prior_plan_unless_explicitly_changed():
    from app.models.schemas import DeckPlan
    from app.services.deck_builder import _next_plan

    prior = DeckPlan(strategy="Proliferate experience counters.", theme_queries=['o:"experience counter"'])
    notes = []
    # The LLM restating (or drifting) the plan without "changed" keeps the original.
    assert _next_plan(prior, {"plan": {"changed": False, "strategy": "Generic goodstuff."}}, notes) == prior
    assert _next_plan(prior, {}, notes) == prior
    assert _next_plan(prior, None, notes) == prior
    assert notes == []
    # An explicit change is adopted and reported.
    changed = _next_plan(prior, {"plan": {"changed": True, "strategy": "Superfriends.", "theme_queries": ["t:planeswalker"]}}, notes)
    assert changed.strategy == "Superfriends." and changed.theme_queries == ["t:planeswalker"]
    assert any("Game plan updated" in n for n in notes)
    # Imported decks (no prior plan) adopt the inferred one.
    inferred = _next_plan(None, {"plan": {"changed": False, "strategy": "Infect.", "theme_queries": ["kw:infect"]}}, [])
    assert inferred.strategy == "Infect." and inferred.theme_queries == ["kw:infect"]


@pytest.mark.asyncio
async def test_build_returns_its_plan_and_revamp_sends_it_back_with_theme_candidates():
    from app.models.schemas import DeckPlan

    class _RecordingLLM(_StubLLM):
        async def plan_deck(self, **kwargs):
            return {"strategy": "Proliferate experience counters.", "targets": {}, "theme_queries": ['o:"experience counter"']}

        async def revamp_deck(self, **kwargs):
            self.revamp_kwargs = kwargs
            return {
                "decklist": [{"name": "Sol Ring", "count": 1}],
                "explanation": "Added removal.",
                "substitutions": [],
                "plan": {"changed": False, "strategy": "Now a generic deck."},
            }

    llm = _RecordingLLM(payload={"decklist": [{"name": "Card 1"}], "explanation": "x"})
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), llm)
    built = await builder.build(BuildDeckRequest(commander="Atraxa", bracket=3, prompt="experience counters"))
    assert built.plan == DeckPlan(strategy="Proliferate experience counters.", theme_queries=['o:"experience counter"'])

    revised = await builder.revamp(
        RevampDeckRequest(
            commander="Atraxa",
            bracket=3,
            prompt="experience counters",
            previous_decklist=[MagicCard(name="Card 1", count=1)],
            change_request="more removal",
            plan=built.plan,
        )
    )
    assert llm.revamp_kwargs["plan_strategy"] == "Proliferate experience counters."
    # Theme candidates come from the plan's searches and skip cards already in the deck.
    assert "Card 2" in llm.revamp_kwargs["theme_candidates"]
    assert "Card 1 " not in llm.revamp_kwargs["theme_candidates"]
    assert revised.plan == built.plan  # not explicitly changed, so the plan is kept


# --- swaps must match the deck ------------------------------------------------------------


@pytest.mark.asyncio
async def test_revamp_applies_listed_swaps_the_decklist_missed():
    # The LLM says "Old Card -> Sol Ring" but returns a decklist that still has Old Card
    # and never added Sol Ring - the bug where the UI showed a replacement that didn't happen.
    previous = [MagicCard(name="Old Card", count=1), MagicCard(name="Keeper", count=1)]
    payload = {
        "decklist": [{"name": "Old Card", "count": 1}, {"name": "Keeper", "count": 1}],
        "explanation": "Revised.",
        "substitutions": [
            {"removed": [{"name": "Old Card", "count": 1}], "added": [{"name": "Sol Ring", "count": 1}], "explanation": "Ramp."}
        ],
    }
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload))
    result = await builder.revamp(
        RevampDeckRequest(commander="Atraxa", bracket=3, previous_decklist=previous, change_request="more ramp")
    )
    names = {c.name for c in result.decklist}
    assert "Old Card" not in names
    assert "Sol Ring" in names
    assert [(s.removed[0].name, s.added[0].name) for s in result.substitutions] == [("Old Card", "Sol Ring")]


def test_reconcile_only_shows_swap_parts_that_happened():
    from app.models.schemas import DeckSubstitution
    from app.services.deck_builder import _reconcile_substitutions

    previous = [{"name": "Old Card", "count": 1}, {"name": "Still Here", "count": 1}, {"name": "Forest", "count": 10}]
    final = [MagicCard(name="Still Here", count=1), MagicCard(name="New Card", count=1), MagicCard(name="Forest", count=9)]
    subs = [
        DeckSubstitution(removed=[MagicCard(name="Old Card")], added=[MagicCard(name="New Card")], explanation="ok"),
        DeckSubstitution(removed=[MagicCard(name="Still Here")], added=[MagicCard(name="Never Added")], explanation="didn't happen"),
        DeckSubstitution(removed=[MagicCard(name="Forest")], added=[MagicCard(name="Ghost")], explanation="half happened"),
    ]
    kept = _reconcile_substitutions(subs, previous, final)
    assert [[c.name for c in s.removed] for s in kept] == [["Old Card"], ["Forest"]]
    assert [[c.name for c in s.added] for s in kept] == [["New Card"], []]


def test_swaps_removing_cards_not_in_the_deck_are_skipped_when_they_overfill_it():
    from app.services.deck_builder import _apply_listed_swaps

    previous = [{"name": f"Card {i}", "count": 1} for i in range(4)]
    llm_result = {
        # 4-card "deck": Card 0 was swapped for New A (fine); "Ghost" was never in the
        # deck, so swapping it for New B would make 5 cards.
        "decklist": [{"name": "New A"}, {"name": "Card 1"}, {"name": "Card 2"}, {"name": "Card 3"}, {"name": "New B"}],
        "substitutions": [
            {"removed": [{"name": "Card 0"}], "added": [{"name": "New A"}], "explanation": "real"},
            {"removed": [{"name": "Ghost"}], "added": [{"name": "New B"}], "explanation": "hallucinated"},
        ],
    }
    result = _apply_listed_swaps(llm_result, previous, deck_size=4)
    names = [e["name"] for e in result["decklist"]]
    assert "New B" not in names and "New A" in names and len(names) == 4
    assert [s["explanation"] for s in result["substitutions"]] == ["real"]
    assert any("Evened out 1 uneven swap" in n and "Ghost" in n for n in result["notes"])


@pytest.mark.asyncio
async def test_revamp_applies_only_listed_swaps_ignoring_a_drifting_full_list():
    # The LLM returns swaps AND a full list that silently drops a land and adds a
    # stowaway. Only the swap may change the deck.
    previous = [MagicCard(name=f"Card {i}", count=1) for i in range(63)] + [MagicCard(name="Plains", count=36)]
    payload = {
        "decklist": [{"name": f"Card {i}", "count": 1} for i in range(1, 63)]
        + [{"name": "New Card", "count": 1}, {"name": "Stowaway", "count": 1}, {"name": "Plains", "count": 35}],
        "explanation": "Revised.",
        "substitutions": [{"removed": [{"name": "Card 0"}], "added": [{"name": "New Card"}], "explanation": "upgrade"}],
    }
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload))
    result = await builder.revamp(
        RevampDeckRequest(commander="Atraxa", bracket=3, previous_decklist=previous, change_request="one upgrade")
    )
    counts = {c.name: c.count for c in result.decklist}
    assert "New Card" in counts and "Card 0" not in counts
    assert "Stowaway" not in counts  # not part of any swap
    assert sum(c.count for c in result.decklist if c.name in {"Plains", "Island", "Swamp", "Forest"}) == 36
    assert not any("Trimmed" in n for n in result.notes)


def test_land_cutting_swaps_are_undone_before_cutting_unrelated_spells():
    from app.models.schemas import DeckSubstitution
    from app.services.deck_builder import _revert_land_cutting_swaps

    deck = [MagicCard(name=f"Spell {i}", count=1, type_line="Sorcery") for i in range(64)]
    deck += [MagicCard(name="Mountain", count=33, type_line="Basic Land - Mountain"), MagicCard(name="Draw Spell", count=1, type_line="Sorcery"), MagicCard(name="Other New", count=1, type_line="Sorcery")]
    subs = [
        DeckSubstitution(removed=[MagicCard(name="Spell 99")], added=[MagicCard(name="Other New")], explanation="spell for spell"),
        DeckSubstitution(removed=[MagicCard(name="Rugged Highlands")], added=[MagicCard(name="Draw Spell")], explanation="land for spell"),
        DeckSubstitution(removed=[MagicCard(name="Mountain")], added=[MagicCard(name="Ghost Draw")], explanation="basic for spell"),
    ]
    data = {"rugged highlands": {"name": "Rugged Highlands", "type_line": "Land", "id": "rh"}}
    notes = []
    # 33 lands; two land->spell swaps exist, the later (Mountain) one is undone first,
    # then Rugged Highlands - stopping as soon as the deck is back at 35.
    deck, kept = _revert_land_cutting_swaps(deck, subs, data, notes)
    names = {c.name: c.count for c in deck}
    assert names["Mountain"] == 34 and names.get("Rugged Highlands") == 1
    assert "Draw Spell" not in names
    assert [s.explanation for s in kept] == ["spell for spell"]
    assert any("Undid 2 swap(s)" in n for n in notes)


def test_uneven_swap_drops_its_extra_addition_instead_of_trimming_other_cards():
    from app.services.deck_builder import _apply_listed_swaps

    previous = [{"name": f"Card {i}", "count": 1} for i in range(4)]
    llm_result = {
        "decklist": [dict(c) for c in previous],
        "substitutions": [
            {"removed": [{"name": "Card 0"}], "added": [{"name": "New A"}, {"name": "New B"}], "explanation": "2 for 1"},
        ],
    }
    result = _apply_listed_swaps(llm_result, previous, deck_size=4)
    names = sorted(e["name"] for e in result["decklist"])
    assert names == ["Card 1", "Card 2", "Card 3", "New A"]
    assert [c["name"] for c in result["substitutions"][0]["added"]] == ["New A"]
    assert any("New B (it added more cards than it removed)" in n for n in result["notes"])


def test_fit_deck_size_trims_basics_above_the_floor_and_pads_when_short():
    from app.services.deck_builder import _fit_deck_size

    deck = [MagicCard(name=f"S{i}", type_line="Sorcery") for i in range(63)] + [MagicCard(name="Mountain", count=37, type_line="Basic Land - Mountain")]
    deck = _fit_deck_size(deck, 99, {"R"}, [])
    assert sum(c.count for c in deck) == 99 and next(c for c in deck if c.name == "Mountain").count == 36
    short = [MagicCard(name=f"S{i}", type_line="Sorcery") for i in range(60)]
    short = _fit_deck_size(short, 99, {"R"}, [])
    assert sum(c.count for c in short) == 99 and next(c for c in short if c.name == "Mountain").count == 39


def test_swap_removing_more_than_it_adds_keeps_the_extra_card():
    from app.services.deck_builder import _apply_listed_swaps

    previous = [{"name": f"Card {i}", "count": 1} for i in range(4)]
    llm_result = {
        "decklist": [dict(c) for c in previous],
        "substitutions": [
            {"removed": [{"name": "Card 0"}, {"name": "Card 1"}], "added": [{"name": "New A"}], "explanation": "1 for 2"},
        ],
    }
    result = _apply_listed_swaps(llm_result, previous, deck_size=4)
    # The last-listed removal (Card 1) is put back; the swap becomes Card 0 -> New A.
    assert sorted(e["name"] for e in result["decklist"]) == ["Card 1", "Card 2", "Card 3", "New A"]
    assert [c["name"] for c in result["substitutions"][0]["removed"]] == ["Card 0"]
    assert any("Evened out 1 uneven swap" in n and "kept Card 1" in n for n in result["notes"])


# --- budgets and concept adherence --------------------------------------------------------


class _PricedScryfall(_StubScryfall):
    """Stub with prices: everything $0.10, basics $0.25, plus a few named pricey cards."""

    PRICES = {"Pricey": "80.00", "Pricey Ramp": "30.00"}

    async def get_collection(self, names, count=True):
        cards = await super().get_collection(names, count)
        for card in cards:
            if card["name"] in _BASICS:
                card["type_line"] = f"Basic Land - {card['name']}"
                card["prices"] = {"usd": "0.25"}
            else:
                card["prices"] = {"usd": self.PRICES.get(card["name"], "0.10")}
        return cards


_BASICS = {"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"}


def _priced_deck(extra):
    """A 99-card Atraxa deck: Card 0..N cheap spells, the extra cards, and Forests."""
    spells = [MagicCard(name=f"Card {i}", count=1, category="Theme", price="0.10") for i in range(60 - len(extra))]
    return spells + extra + [MagicCard(name="Forest", count=39, category="Land", price="0.25")]


@pytest.mark.asyncio
async def test_build_with_budget_passes_it_on_and_reports_the_real_total():
    from app.services.budget import deck_total

    class _BudgetLLM(_StubLLM):
        async def plan_deck(self, **kwargs):
            self.plan_kwargs = kwargs
            return {"strategy": "Clones.", "targets": {}, "theme_queries": ["otag:clone"], "avoid_queries": ["t:god", "otag:clone"]}

        async def select_cards(self, **kwargs):
            self.select_kwargs = kwargs
            return {"picks": ["Card 1"], "explanation": "x", "notes": ["Total is $12, under budget.", "Clones!"]}

    llm = _BudgetLLM(payload={})
    builder = DeckBuilder(_PricedScryfall(), _StubEDHRec(), llm)
    deck = await builder.build(BuildDeckRequest(commander="Atraxa", bracket=3, prompt="clones only, budget around $60"))
    assert llm.plan_kwargs["budget"] == 60.0 and llm.select_kwargs["budget"] == 60.0
    assert "$0.10" in llm.select_kwargs["pool_text"]  # prices shown to the model
    assert deck_total(deck.decklist) <= 60.0
    assert deck.plan.budget == 60.0
    assert deck.plan.avoid_queries == ["t:god"]  # an avoid query equal to a theme query is dropped
    assert "Clones!" in deck.notes and not any("$12" in n for n in deck.notes)
    assert any(n.startswith("Budget: the 99 cost $") for n in deck.notes)


@pytest.mark.asyncio
async def test_revamp_undoes_swaps_that_break_the_budget():
    from app.models.schemas import DeckPlan

    previous = _priced_deck([MagicCard(name="Old Card", count=1, category="Theme", price="0.10")])
    payload = {
        "explanation": "Upgraded.",
        "substitutions": [
            {"removed": [{"name": "Old Card"}], "added": [{"name": "Pricey"}], "explanation": "Stronger."},
            {"removed": [{"name": "Card 0"}], "added": [{"name": "Card 70"}], "explanation": "Cheap and fine."},
        ],
    }
    llm = _StubLLM(payload)
    builder = DeckBuilder(_PricedScryfall(), _StubEDHRec(), llm)
    result = await builder.revamp(
        RevampDeckRequest(
            commander="Atraxa",
            bracket=3,
            previous_decklist=previous,
            change_request="make it stronger",
            plan=DeckPlan(strategy="Clones.", budget=40.0),
        )
    )
    names = {c.name for c in result.decklist}
    assert "Pricey" not in names and "Old Card" in names  # costly swap undone
    assert "Card 70" in names and "Card 0" not in names  # cheap swap kept
    assert [(s.removed[0].name, s.added[0].name) for s in result.substitutions] == [("Card 0", "Card 70")]
    assert any("undid 1 swap" in n and "Pricey" in n for n in result.notes)
    assert result.plan.budget == 40.0  # carried to the next revision


@pytest.mark.asyncio
async def test_revamp_gets_an_over_budget_deck_under_a_new_budget_with_visible_swaps():
    from app.services.budget import deck_total

    class _RecordingLLM(_StubLLM):
        async def revamp_deck(self, **kwargs):
            self.kwargs = kwargs
            return {"explanation": "No changes.", "substitutions": []}

    previous = _priced_deck([MagicCard(name="Pricey Ramp", count=1, category="Ramp")])
    llm = _RecordingLLM(payload={})
    builder = DeckBuilder(_PricedScryfall(), _StubEDHRec(), llm)
    result = await builder.revamp(
        RevampDeckRequest(
            commander="Atraxa", bracket=3, previous_decklist=previous, change_request="get it under $25"
        )
    )
    # Prices were looked up for the prompt (Pricey Ramp had none in the request).
    assert llm.kwargs["budget"] == 25.0 and llm.kwargs["current_total"] > 25.0
    assert deck_total(result.decklist) <= 25.0
    assert "Pricey Ramp" not in {c.name for c in result.decklist}
    budget_swaps = [s for s in result.substitutions if s.explanation.startswith("Budget:")]
    assert [s.removed[0].name for s in budget_swaps] == ["Pricey Ramp"]
    assert result.plan.budget == 25.0


@pytest.mark.asyncio
async def test_revamp_can_lift_the_budget():
    from app.models.schemas import DeckPlan

    builder = DeckBuilder(_PricedScryfall(), _StubEDHRec(), _StubLLM({"explanation": "x", "substitutions": []}))
    result = await builder.revamp(
        RevampDeckRequest(
            commander="Atraxa",
            bracket=3,
            previous_decklist=_priced_deck([]),
            change_request="ignore the budget, make it stronger",
            plan=DeckPlan(strategy="Clones.", budget=40.0),
        )
    )
    assert result.plan.budget is None


def test_a_card_added_by_several_swaps_is_only_added_once():
    from app.services.deck_builder import _apply_listed_swaps

    previous = [{"name": f"Card {i}", "count": 1} for i in range(4)]
    llm_result = {
        "decklist": [dict(c) for c in previous],
        "substitutions": [
            {"removed": [{"name": "Card 0"}], "added": [{"name": "New A"}], "explanation": "a"},
            {"removed": [{"name": "Card 1"}], "added": [{"name": "New A"}], "explanation": "again"},
            {"removed": [{"name": "Card 2"}], "added": [{"name": "Card 3"}], "explanation": "already in"},
        ],
    }
    result = _apply_listed_swaps(llm_result, previous, deck_size=4)
    assert sorted(e["name"] for e in result["decklist"]) == ["Card 1", "Card 2", "Card 3", "New A"]
    assert any("already has or another swap already adds: New A, Card 3" in n for n in result["notes"])


def test_reconcile_credits_each_added_copy_to_one_swap_only():
    from app.models.schemas import DeckSubstitution
    from app.services.deck_builder import _reconcile_substitutions

    previous = [{"name": "A", "count": 1}, {"name": "B", "count": 1}]
    final = [MagicCard(name="B"), MagicCard(name="New")]
    subs = [
        DeckSubstitution(removed=[MagicCard(name="A")], added=[MagicCard(name="New")], explanation="1"),
        DeckSubstitution(removed=[MagicCard(name="B")], added=[MagicCard(name="New")], explanation="2"),
    ]
    kept = _reconcile_substitutions(subs, previous, final)
    assert [(len(s.removed), len(s.added)) for s in kept] == [(1, 1)]


def test_costly_swap_keeps_its_job_with_a_cheaper_card_before_being_undone():
    from app.models.schemas import DeckSubstitution
    from app.services.card_pool import CardPool
    from app.services.deck_builder import _revert_costly_swaps

    deck = [
        MagicCard(name="Keeper", type_line="Creature", price="1.00"),
        MagicCard(name="Pricey Removal", category="Swap", type_line="Instant", price="9.00",
                  oracle_text="Destroy target artifact."),
        MagicCard(name="Forest", count=10, type_line="Basic Land - Forest", price="0.25"),
    ]
    sub = DeckSubstitution(
        removed=[MagicCard(name="Old Spell")], added=[MagicCard(name="Pricey Removal")], explanation="More removal."
    )
    pool = CardPool()
    pool.add({"name": "Cheap Removal", "type_line": "Instant", "prices": {"usd": "0.30"}}, "removal")
    pool.add({"name": "Cheap Draw", "type_line": "Sorcery", "prices": {"usd": "0.10"}}, "draw")
    card_data = {  # as validated during the revamp: the old deck plus every swap addition
        "old spell": {"name": "Old Spell", "type_line": "Sorcery", "prices": {"usd": "0.50"}},
        "pricey removal": {"name": "Pricey Removal", "type_line": "Instant", "prices": {"usd": "9.00"}},
    }
    notes = []
    deck, subs, stand_ins = _revert_costly_swaps(deck, [sub], card_data, 5.0, notes, candidates=pool.cards)
    assert [c.name for c in stand_ins] == ["Cheap Removal"]  # same job (role read from rules text)
    assert "Cheap Removal" in {c.name for c in deck} and "Pricey Removal" not in {c.name for c in deck}
    assert [c.name for c in subs[0].added] == ["Cheap Removal"] and "didn't fit" in subs[0].explanation
    assert any("used cheaper cards that do the same job" in n for n in notes)
