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
    "mana_cost": "{G}{W}{U}{B}",
}

MONOR = {
    "id": "def-456",
    "name": "Krenko, Mob Boss",
    "type_line": "Legendary Creature - Goblin Warrior",
    "oracle_text": "Tap: Create X 1/1 red Goblin tokens.",
    "legalities": {"commander": "legal"},
    "color_identity": ["R"],
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
    async def resolve_commander(self, name):
        return ATRAXA

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
    }
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload))
    result = await builder.build(BuildDeckRequest(commander="Atraxa", bracket=3, prompt="combo"))
    assert result.source == "llm"
    assert result.version == 1
    assert result.bracket == 3
    assert sum(c.count for c in result.decklist) == DECK_SIZE
    assert result.explanation == "Test plan."
    assert "Test note." in result.notes


@pytest.mark.asyncio
async def test_build_without_llm_uses_heuristic():
    builder = DeckBuilder(_StubScryfall(), _StubEDHRec(), _StubLLM(payload=None, enabled=False))
    result = await builder.build(BuildDeckRequest(commander="Atraxa", bracket=2, prompt=""))
    assert result.source == "heuristic"
    assert sum(c.count for c in result.decklist) == DECK_SIZE
    names = {c.name for c in result.decklist}
    assert "Sol Ring" in names or "Command Tower" in names


@pytest.mark.asyncio
async def test_revamp_increments_version():
    payload = {
        "decklist": [{"name": "Sol Ring", "count": 1}],
        "explanation": "Revised.",
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
    assert result.version == 2
    assert any("LLM unavailable" in n for n in result.notes)


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
