"""Deck budgets: read a dollar cap from the user's words and enforce it in code.

A budget is treated as a hard cap on the 99 (the commanders are not counted - the
same total the UI shows as "Total price"). Prompts tell the LLM about it, but the
model's own arithmetic is not trusted: after a build or revision, ``enforce_budget``
swaps the most expensive cards for cheaper candidates that do the same job until the
deck fits.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from ..models.schemas import MagicCard
from .card_pool import BASIC_BY_COLOR, PoolCard, _to_deck_card

# Anything below this can't buy 99 cards, so a smaller number is not a budget.
MIN_BUDGET = 10.0
MAX_BUDGET = 100_000.0
# Per-card ceiling for build candidates: no single card may eat more than this share
# of the budget, so the model is never offered a card that would blow it.
MAX_CARD_SHARE = 0.10
MIN_CARD_CAP = 1.0
# Price assumed for a basic land before it is hydrated.
BASIC_PRICE_ESTIMATE = 0.30

_BASIC_NAMES = {*BASIC_BY_COLOR.values(), "Wastes"}
_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s*(k\b)?"
_PATTERNS = [
    re.compile(r"\$\s*" + _NUMBER, re.IGNORECASE),  # $100, $ 1,500, $1k
    re.compile(_NUMBER + r"\s*(?:usd|dollars?|bucks)\b", re.IGNORECASE),  # 100 dollars
    re.compile(
        r"\bbudget\s*(?:of|is|at|:|=|around|about|near|under|below|max|maximum|cap|limit|"
        r"range|of\s+about|of\s+around|under\s+about)?\s*(?:of\s*)?\$?\s*" + _NUMBER,
        re.IGNORECASE,
    ),  # budget of 150, budget: 150, budget around 100
    re.compile(
        r"\b(?:under|below|less\s+than|max(?:imum)?|cap(?:ped)?\s+at|no\s+more\s+than)\s+\$?\s*"
        + _NUMBER
        + r"\s*(?:budget|total|deck)",
        re.IGNORECASE,
    ),  # under 100 budget
]
_NO_BUDGET = re.compile(
    r"\b(?:no|without|ignore|ignoring|drop|remove|lift|forget|unlimited|any)\s+(?:the\s+|a\s+)?budget\b"
    r"|\bbudget\s+(?:doesn'?t|does\s+not|no\s+longer)\s+matters?\b",
    re.IGNORECASE,
)


def parse_budget(text: Optional[str]) -> Optional[float]:
    """Return the dollar cap the text asks for, or None.

    "around the 100 dollar budget range" -> 100.0; "budget under $1.5k" -> 1500.0.
    The first plausible amount wins. Bare numbers ("bracket 3", "+1/+1") never count.
    """
    if not text or _NO_BUDGET.search(text):
        return None
    hits: list[tuple[int, float]] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            whole, cents, thousands = match.group(1), match.group(2), match.group(3)
            amount = float(whole.replace(",", "") + (f".{cents}" if cents else ""))
            if thousands:
                amount *= 1000
            if MIN_BUDGET <= amount <= MAX_BUDGET:
                hits.append((match.start(), amount))
    return min(hits)[1] if hits else None


def removes_budget(text: Optional[str]) -> bool:
    """True when the text lifts the budget ("no budget", "ignore the budget")."""
    return bool(text and _NO_BUDGET.search(text))


def price_of(card: Any) -> Optional[float]:
    """USD price of one copy: a MagicCard's ``price`` or a Scryfall dict's ``prices``."""
    if isinstance(card, MagicCard):
        raw: Any = card.price
    elif isinstance(card, PoolCard):
        return price_of(card.data)
    elif isinstance(card, dict):
        prices = card.get("prices")
        if isinstance(prices, dict):
            raw = prices.get("usd") or prices.get("usd_foil") or prices.get("usd_etched")
        else:
            raw = card.get("price")
    else:
        return None
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _line_price(card: MagicCard) -> float:
    price = price_of(card)
    if price is None and card.name in _BASIC_NAMES:
        price = BASIC_PRICE_ESTIMATE
    return (price or 0.0) * max(1, card.count or 1)


def deck_total(decklist: Iterable[MagicCard]) -> float:
    """Total price of a decklist (cards with no known price count as $0)."""
    return round(sum(_line_price(card) for card in decklist), 2)


def card_price_cap(budget: Optional[float]) -> Optional[float]:
    """The most a single candidate may cost for a deck with this budget."""
    if budget is None:
        return None
    return max(MIN_CARD_CAP, round(budget * MAX_CARD_SHARE, 2))


def budget_instructions(budget: Optional[float], current_total: Optional[float] = None) -> str:
    """Prompt text for a hard budget (empty when there is none)."""
    if budget is None:
        return ""
    per_card = budget / 99
    text = (
        f"HARD BUDGET: the 99 cards (commanders not counted) must total at most ${budget:.2f} "
        f"- about ${per_card:.2f} per card on average. Card prices are shown in USD. This is "
        "an absolute cap: prefer cheaper cards that do the same job, and spend the money on "
        "the cards that matter most to the concept. The server checks the real total and "
        "replaces the most expensive cards if the deck goes over.\n"
    )
    if current_total is not None:
        headroom = budget - current_total
        text += (
            f"The deck currently costs ${current_total:.2f}, so "
            + (
                f"your swaps may add at most ${headroom:.2f} in total (added cards' prices minus "
                "removed cards' prices).\n"
                if headroom >= 0
                else f"it is ${-headroom:.2f} OVER budget - your swaps must bring it under "
                "by replacing expensive cards with cheaper ones that do the same job.\n"
            )
        )
    return text


_CATEGORY_ROLES = {
    "ramp": "ramp",
    "card draw": "draw",
    "draw": "draw",
    "removal": "removal",
    "board wipe": "wipe",
    "wipe": "wipe",
    "theme": "theme",
    "synergy": "synergy",
    "land": "land",
}


def _is_land(card: MagicCard) -> bool:
    if card.name in _BASIC_NAMES:
        return True
    return "Land" in (card.type_line or "").split("//")[0]


# Rough role guesses from rules text, for cards with no pool role or useful category
# (e.g. a card a revision just added, categorized only as "Swap").
_TEXT_ROLES = [
    ("wipe", re.compile(r"\b(destroy|exile) all\b|each creature gets -|deals? \d+ damage to each creature", re.I)),
    ("removal", re.compile(r"\b(destroy|exile) (up to \w+ )?target\b|counter target|deals? \d+ damage to (any|target)|return target .* to its owner's hand", re.I)),
    ("draw", re.compile(r"\bdraws? (a|two|three|\w+) cards?\b|\bdraw cards?\b", re.I)),
    ("ramp", re.compile(r"\badd \{|search your library for (a|up to \w+) .*land", re.I)),
]


def _roles_of(card: MagicCard, pool_roles: dict[str, set[str]]) -> set[str]:
    roles = set(pool_roles.get(card.name.lower(), set()))
    category_role = _CATEGORY_ROLES.get((card.category or "").strip().lower())
    if category_role:
        roles.add(category_role)
    if _is_land(card):
        roles.add("land")
    if not roles - {"synergy"} and card.oracle_text:
        roles.update(role for role, pattern in _TEXT_ROLES if pattern.search(card.oracle_text))
    return roles


def enforce_budget(
    decklist: list[MagicCard],
    budget: Optional[float],
    candidates: Iterable[PoolCard],
    identity: set[str],
    excluded: Iterable[str] = (),
    max_lands: int = 40,
    protected: Iterable[str] = (),
) -> tuple[list[tuple[MagicCard, MagicCard]], float]:
    """Swap the most expensive cards for cheaper ones until the deck fits the budget.

    Each step replaces the priciest non-basic card with the best candidate that costs
    less and, where possible, does the same job: a candidate sharing one of its roles
    (ramp, draw, removal, wipe, theme, land), on-theme over generic, and priced near the
    average per-card budget. A land with no cheaper land candidate becomes a basic.
    Every step lowers the total, so the loop always ends.

    Mutates ``decklist`` in place and returns ``([(removed, added), ...], final_total)``;
    added cards are not hydrated yet (the caller does that).
    """
    total = deck_total(decklist)
    if budget is None or total <= budget:
        return [], total

    in_deck = {card.name.lower() for card in decklist}
    blocked = {name.strip().lower() for name in excluded} | in_deck
    pool = [c for c in candidates if c.key not in blocked and price_of(c) is not None]
    pool_roles = {c.key: set(c.roles) for c in candidates}
    colors = [c for c in "WUBRG" if c in identity] or ["C"]
    per_card = budget / 99
    swaps: list[tuple[MagicCard, MagicCard]] = []
    keep = {name.strip().lower() for name in protected}

    for _ in range(200):
        if total <= budget:
            break
        lands = sum(max(1, c.count or 1) for c in decklist if _is_land(c))
        # Priciest single copies first; basics are already as cheap as cards get.
        priced = sorted(
            (
                c for c in decklist
                if c.name not in _BASIC_NAMES and (price_of(c) or 0) > 0 and c.name.lower() not in keep
            ),
            key=lambda c: price_of(c) or 0,
            reverse=True,
        )
        replaced = False
        for card in priced:
            current = price_of(card) or 0.0
            roles = _roles_of(card, pool_roles)
            land = _is_land(card)
            options = [
                c for c in pool
                if c.key not in in_deck and (price_of(c) or 0) < current and c.is_land == land
            ]

            def rank(c: PoolCard) -> tuple:
                price = price_of(c) or 0.0
                return (
                    bool(roles & c.roles - {"synergy"}),  # does the same job
                    "theme" in c.roles,  # on-concept over generic
                    price <= per_card,  # fits the average card budget
                    c.score(),
                )

            options.sort(key=rank, reverse=True)
            replacement: Optional[MagicCard] = None
            if options:
                pick = options[0]
                replacement = _to_deck_card(pick)
                in_deck.add(pick.key)
            elif land or lands < max_lands:
                basic_color = colors[len(swaps) % len(colors)]
                replacement = MagicCard(
                    name=BASIC_BY_COLOR.get(basic_color, "Wastes"),
                    count=1,
                    category="Land",
                    type_line="Basic Land",
                    price=f"{BASIC_PRICE_ESTIMATE:.2f}",
                )
            if replacement is None:
                continue

            removed = card.model_copy(update={"count": 1})
            if (card.count or 1) > 1:
                card.count -= 1
            else:
                decklist.remove(card)
                in_deck.discard(card.name.lower())
            existing = next((c for c in decklist if c.name == replacement.name), None)
            if existing is not None and replacement.name in _BASIC_NAMES:
                existing.count += 1
            else:
                decklist.append(replacement)
            swaps.append((removed, replacement))
            total = deck_total(decklist)
            replaced = True
            break
        if not replaced:
            break
    return swaps, total


def cheaper_alternative(
    card: MagicCard,
    candidates: Iterable[PoolCard],
    blocked: set[str],
    target_price: float,
) -> Optional[PoolCard]:
    """A cheaper candidate that does the same job as ``card`` (shares a role), or None.

    Prefers on-theme cards at or under ``target_price``; failing that, the cheapest
    same-role card that still costs less than ``card``.
    """
    candidates = list(candidates)
    pool_roles = {c.key: set(c.roles) for c in candidates}
    roles = _roles_of(card, pool_roles) - {"synergy"}
    current = price_of(card) or 0.0
    if not roles or current <= 0:
        return None
    land = _is_land(card)
    options = [
        c for c in candidates
        if c.key not in blocked
        and c.key != card.name.lower()
        and c.is_land == land
        and roles & c.roles
        and (price_of(c) is not None and price_of(c) < current)
    ]
    fitting = [c for c in options if (price_of(c) or 0) <= target_price]
    if fitting:
        return max(fitting, key=lambda c: ("theme" in c.roles, c.score()))
    return min(options, key=lambda c: price_of(c) or 0) if options else None


def replacement_for(
    card: MagicCard,
    candidates: Iterable[PoolCard],
    blocked: set[str],
    max_price: Optional[float] = None,
) -> Optional[PoolCard]:
    """The best candidate to take ``card``'s slot: same job first, then on-theme, then score.

    Used when a card must go (the user asked to cut it) or a suggested addition isn't
    allowed (the user removed it before). Lands are only replaced by lands and spells by
    spells; ``max_price`` (when set) keeps the stand-in within the budget's per-card cap.
    """
    candidates = list(candidates)
    pool_roles = {c.key: set(c.roles) for c in candidates}
    roles = _roles_of(card, pool_roles) - {"synergy"}
    land = _is_land(card)
    options = [
        c for c in candidates
        if c.key not in blocked
        and c.key != card.name.lower()
        and c.is_land == land
        and (max_price is None or (price_of(c) or 0.0) <= max_price)
    ]
    if not options:
        return None
    return max(options, key=lambda c: (bool(roles & c.roles), "theme" in c.roles, c.score()))


def budget_note(budget: Optional[float], total: float, swaps: list[tuple[MagicCard, MagicCard]]) -> list[str]:
    """User-facing notes: the real total against the cap, plus any budget swaps made."""
    if budget is None:
        return []
    notes: list[str] = []
    if swaps:
        listed = ", ".join(
            f"{old.name} (${price_of(old) or 0:.2f}) -> {new.name}" for old, new in swaps[:10]
        )
        notes.append(
            f"Budget: swapped {len(swaps)} card(s) for cheaper ones that do the same job to stay "
            f"under ${budget:.2f}: {listed}{'...' if len(swaps) > 10 else ''}."
        )
    if total <= budget:
        notes.append(f"Budget: the 99 cost ${total:.2f} of your ${budget:.2f} cap (commanders not counted).")
    else:
        notes.append(
            f"Budget: the 99 cost ${total:.2f}, still over your ${budget:.2f} cap - no cheaper "
            "replacements were found for the remaining cards."
        )
    return notes


def strip_llm_budget_claims(notes: list[str]) -> list[str]:
    """Drop the model's own budget arithmetic; it is often wrong and the server reports the real total."""
    return [n for n in notes if not re.search(r"budget|\$\s*\d", n, re.IGNORECASE)]
