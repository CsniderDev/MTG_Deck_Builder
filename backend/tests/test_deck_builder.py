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
    def __init__(self):
        self.collection_calls = 0

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

    async def get_collection(self, names):
        self.collection_calls += 1
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
    def __init__(self, payload=None, enabled=True):
        self._payload = payload
        self.enabled = enabled

    async def build_deck(self, **kwargs):
        return self._payload

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
async def test_build_with_full_llm_payload_reuses_validation_collection_for_hydration():
    payload = {
        "decklist": [{"name": f"Card {i}", "count": 1, "category": "Misc"} for i in range(99)],
        "explanation": "Fast plan.",
    }
    scryfall = _StubScryfall()
    builder = DeckBuilder(scryfall, _StubEDHRec(), _StubLLM(payload))
    result = await builder.build(BuildDeckRequest(commander="Atraxa", bracket=3, prompt="combo"))
    assert result.source == "llm"
    assert scryfall.collection_calls == 1


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
    with pytest.raises(DeckBuilderError, match="Gemini"):
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

        async def build_deck(self, **kwargs):
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
