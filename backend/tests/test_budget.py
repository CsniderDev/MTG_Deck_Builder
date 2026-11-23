"""Budgets: parsing a cap from the user's words, and enforcing it in code."""
import pytest

from app.models.schemas import MagicCard
from app.services.budget import (
    budget_instructions,
    card_price_cap,
    deck_total,
    enforce_budget,
    parse_budget,
    removes_budget,
    strip_llm_budget_claims,
)
from app.services.card_pool import CardPool, build_card_pool


@pytest.mark.parametrize(
    "text, expected",
    [
        ("I want this around the 100 dollar budget range", 100.0),
        ("Kratos clones, budget around $100.", 100.0),
        ("budget of 150", 150.0),
        ("budget: $75.50 please", 75.5),
        ("keep it under $1,200", 1200.0),
        ("a $1.5k deck", 1500.0),
        ("under 80 bucks", 80.0),
        ("keep it under 60 budget", 60.0),
        ("bracket 3 +1/+1 counters, 35 lands", None),  # bare numbers are not budgets
        ("budget-friendly removal", None),  # no amount
        ("no budget, go wild ($500 cards are fine)", None),
        ("", None),
        ("$5 deck", None),  # can't buy 99 cards for $5
    ],
)
def test_parse_budget(text, expected):
    assert parse_budget(text) == expected


def test_removes_budget_and_instructions():
    assert removes_budget("ignore the budget and make it stronger")
    assert removes_budget("budget doesn't matter anymore")
    assert not removes_budget("stay on budget")
    assert budget_instructions(None) == ""
    over = budget_instructions(100, current_total=120)
    assert "HARD BUDGET" in over and "$20.00 OVER budget" in over
    under = budget_instructions(100, current_total=90)
    assert "at most $10.00 in total" in under
    assert card_price_cap(100) == 10.0 and card_price_cap(5) == 1.0 and card_price_cap(None) is None


def test_strip_llm_budget_claims_keeps_other_notes():
    notes = ["The cost for the picks totals $97.77, under the $100 budget.", "Leans on clones.", "Budget-conscious picks."]
    assert strip_llm_budget_claims(notes) == ["Leans on clones."]


def _pool(entries):
    pool = CardPool()
    for name, price, role, type_line in entries:
        card = pool.add(
            {"name": name, "type_line": type_line, "prices": {"usd": price}, "edhrec_rank": 100},
            role,
        )
        if "Land" in type_line:
            card.roles.add("land")
    return pool


def test_enforce_budget_swaps_priciest_cards_for_cheaper_ones_doing_the_same_job():
    deck = [
        MagicCard(name="Pricey Ramp", category="Ramp", type_line="Artifact", price="40.00"),
        MagicCard(name="Pricey Land", category="Land", type_line="Land", price="30.00"),
        MagicCard(name="Fine Card", category="Theme", type_line="Creature", price="5.00"),
        MagicCard(name="Island", count=10, category="Land", type_line="Basic Land - Island", price="0.25"),
    ]
    pool = _pool(
        [
            ("Cheap Ramp", "0.50", "ramp", "Artifact"),
            ("Cheap Draw", "0.20", "draw", "Sorcery"),  # cheaper, but a different job
            ("Pricier Ramp", "45.00", "ramp", "Artifact"),  # never a "replacement"
        ]
    )
    swaps, total = enforce_budget(deck, 20.0, pool.cards, {"U"})
    pairs = [(old.name, new.name) for old, new in swaps]
    assert pairs == [("Pricey Ramp", "Cheap Ramp"), ("Pricey Land", "Island")]  # land -> basic
    assert total <= 20.0
    assert deck_total(deck) == total
    names = {c.name for c in deck}
    assert {"Cheap Ramp", "Fine Card"} <= names and "Pricey Ramp" not in names
    assert next(c for c in deck if c.name == "Island").count == 11  # merged into the pile


def test_enforce_budget_does_nothing_under_budget_or_without_one():
    deck = [MagicCard(name="Card", type_line="Artifact", price="5.00")]
    assert enforce_budget(deck, None, [], set()) == ([], 5.0)
    assert enforce_budget(deck, 10.0, [], set()) == ([], 5.0)


def test_enforce_budget_prefers_on_theme_replacements():
    deck = [MagicCard(name="Pricey Clone", category="Theme", type_line="Creature", price="25.00")]
    pool = _pool([("Generic Beater", "0.20", "synergy", "Creature"), ("Cheap Clone", "0.40", "theme", "Creature")])
    swaps, _ = enforce_budget(deck, 10.0, pool.cards, {"U"})
    assert [(o.name, n.name) for o, n in swaps] == [("Pricey Clone", "Cheap Clone")]


class _Scryfall:
    def __init__(self, responses):
        self.responses = responses

    async def search(self, query, order="edhrec", direction="auto", limit=175):
        for needle, cards in self.responses.items():
            if needle in query and "new:card" not in query:
                return cards[:limit]
        return []

    async def get_collection(self, names):
        return [_data(name) for name in names]  # EDHREC cards


def _data(name, price="0.50", type_line="Creature"):
    return {
        "name": name,
        "type_line": type_line,
        "color_identity": [],
        "legalities": {"commander": "legal"},
        "prices": {"usd": price},
    }


@pytest.mark.asyncio
async def test_card_pool_drops_cards_over_the_price_cap_and_off_concept_cards():
    scryfall = _Scryfall(
        {
            "otag:clone": [_data("Cheap Clone"), _data("Pricey Clone", "50.00"), _data("Clone God")],
            "t:god": [_data("Some God"), _data("Clone God")],
            "otag:ramp": [_data("Cheap Rock", type_line="Artifact"), _data("Ramp God")],
        }
    )
    pool = await build_card_pool(
        scryfall,
        [{"name": "Kratos", "color_identity": ["R", "W"]}],
        {"Synergy": [{"name": "Some God"}]},
        ["otag:clone"],
        set(),
        set(),
        None,
        max_card_price=10.0,
        avoid_queries=["t:god"],
    )
    names = {c.name for c in pool.cards}
    assert "Cheap Clone" in names and "Cheap Rock" in names
    assert "Pricey Clone" not in names  # over the per-card cap
    assert "Some God" not in names  # matched only the avoid search
    assert {"Clone God", "Ramp God"} <= names  # also found by a theme/role search, so kept
    assert pool.rejected["over budget"] == 1 and pool.rejected["off-plan"] >= 1
