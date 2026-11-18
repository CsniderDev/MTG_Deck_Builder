"""Candidate card pool for deck building.

Instead of letting the LLM write card names from memory (which favors old, popular
cards and invents names), every build gathers a numbered pool of real, pre-validated
candidates from several sources:

* EDHREC's commander page - what people actually play with this commander, plus
  "hidden gems" (high synergy, low play rate).
* Scryfall role searches (ramp, card draw, removal, board wipes, mana lands) within the
  deck's color identity, across every legal printing.
* Scryfall theme searches the LLM writes from the user's concept (e.g. ``o:proliferate``),
  each run twice: by popularity, and newest-first restricted to first printings, so new
  sets and Universes Beyond cards show up even if the model has never heard of them.
* The newest cards in the color identity.

Every card is checked for Commander legality, color identity, the banned list and
game-changer status before the LLM sees it. The LLM then picks card numbers, and
``assemble_deck`` turns those picks into an exact-size deck, filling any role the
model left short from the same pool.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from ..models.schemas import MagicCard
from .scryfall import ScryfallError

logger = logging.getLogger(__name__)

BASIC_BY_COLOR = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}

# Role -> (Scryfall query fragment, how many top results to keep). Searches are
# sorted by EDHREC popularity across all commanders, so these are proven staples for
# each job within the deck's colors.
ROLE_SEARCHES: dict[str, tuple[str, int]] = {
    "ramp": ("otag:ramp -t:land", 25),
    "draw": ("otag:card-advantage -t:land", 25),
    "removal": ("otag:removal -t:land", 25),
    "wipe": ("otag:board-wipe -t:land", 10),
    "land": ('t:land -t:basic (o:"add {" or o:"search your library")', 30),
}
THEME_POPULAR_LIMIT = 50  # the tail of this list is where theme-relevant deep cuts live
THEME_NEW_LIMIT = 12
GENERAL_NEW_LIMIT = 15
NEW_CARD_WINDOW_DAYS = 548  # ~18 months
MAX_THEME_QUERIES = 4
# A theme card that isn't on the commander's EDHREC page and is rarely played overall
# is a "gem": relevant to the concept but easy to overlook.
GEM_MIN_EDHREC_RANK = 5000
MAX_THEME_QUERY_CHARS = 120

# Category label shown in the UI, by priority when a card has several roles.
ROLE_LABELS = [
    ("land", "Land"),
    ("wipe", "Board Wipe"),
    ("removal", "Removal"),
    ("draw", "Card Draw"),
    ("ramp", "Ramp"),
    ("theme", "Theme"),
    ("synergy", "Synergy"),
]
FILL_ROLES = ("ramp", "draw", "removal", "wipe")
# Up to this many of the model's own [new]/[gem] picks survive trimming.
EXPLORE_KEEP = 6

DEFAULT_TARGETS = {"lands": 36, "ramp": 10, "draw": 10, "removal": 8, "wipe": 3}

# Scryfall keywords the LLM must not use in theme queries: the server adds its own
# identity / legality filters and ordering, and these would override or widen them.
_FORBIDDEN_QUERY_KEYS = re.compile(
    r"\b(?:id|identity|ci|commander|f|format|legal|banned|restricted|order|unique|"
    r"include|direction|dir|game|in|lang|display|prefer)\s*[:=<>!]",
    re.IGNORECASE,
)


@dataclass
class PoolCard:
    id: int
    data: dict[str, Any]
    roles: set[str] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)  # "new", "gem", "edhrec", "game changer"
    num_decks: int = 0  # commander-specific EDHREC play count, when known

    @property
    def name(self) -> str:
        return self.data.get("name") or ""

    @property
    def key(self) -> str:
        return self.name.lower()

    @property
    def type_line(self) -> str:
        return self.data.get("type_line") or ""

    @property
    def is_land(self) -> bool:
        front = self.type_line.split("//")[0]
        return "Land" in front

    @property
    def is_game_changer(self) -> bool:
        return "game changer" in self.tags

    @property
    def label(self) -> str:
        for role, label in ROLE_LABELS:
            if role in self.roles:
                return label
        return "Synergy"

    def score(self) -> tuple[int, int, int]:
        """Higher is better: commander-specific EDHREC data first, then global rank."""
        rank = self.data.get("edhrec_rank")
        return (1 if self.num_decks else 0, self.num_decks, -(rank if rank is not None else 10**7))


class CardPool:
    def __init__(self) -> None:
        self.cards: list[PoolCard] = []
        self._by_key: dict[str, PoolCard] = {}
        self.rejected: Counter = Counter()

    def __len__(self) -> int:
        return len(self.cards)

    def get(self, ref: Any) -> Optional[PoolCard]:
        """Look a card up by pool number (int or "#12") or by exact name."""
        if isinstance(ref, bool):
            return None
        if isinstance(ref, int):
            return self.cards[ref - 1] if 1 <= ref <= len(self.cards) else None
        text = str(ref or "").strip()
        if text.lstrip("#").isdigit():
            return self.get(int(text.lstrip("#")))
        return self._by_key.get(text.lower())

    def add(self, data: dict[str, Any], role: Optional[str] = None, tag: Optional[str] = None) -> Optional[PoolCard]:
        key = (data.get("name") or "").lower()
        if not key:
            return None
        card = self._by_key.get(key)
        if card is None:
            card = PoolCard(id=len(self.cards) + 1, data=data)
            self.cards.append(card)
            self._by_key[key] = card
        if role:
            card.roles.add(role)
        if tag:
            card.tags.add(tag)
        return card

    def ranked(self, role: Optional[str] = None) -> list[PoolCard]:
        cards = [c for c in self.cards if role is None or role in c.roles]
        return sorted(cards, key=lambda c: c.score(), reverse=True)


def sanitize_theme_queries(queries: Any) -> list[str]:
    """Keep up to ``MAX_THEME_QUERIES`` short Scryfall queries without scope-changing keys."""
    cleaned: list[str] = []
    if not isinstance(queries, list):
        return cleaned
    for raw in queries:
        query = " ".join(str(raw or "").split())[:MAX_THEME_QUERY_CHARS]
        if not query or _FORBIDDEN_QUERY_KEYS.search(query):
            continue
        if query.count('"') % 2 or query.count("(") != query.count(")"):
            continue
        if query not in cleaned:
            cleaned.append(query)
        if len(cleaned) >= MAX_THEME_QUERIES:
            break
    return cleaned


_CONCEPT_STOPWORDS = {
    "a", "an", "the", "and", "or", "with", "deck", "build", "focus", "focused", "focusing",
    "on", "of", "for", "my", "to", "that", "in", "around", "using", "use", "uses", "theme",
    "themed", "strategy", "plan", "lots", "lot", "more", "less", "some", "very", "really",
    "aggressive", "budget", "fast", "slow", "value", "good", "best", "cards", "card", "want",
    "i", "is", "it", "be", "make", "lean", "heavy", "bracket", "casual", "competitive", "cedh",
}


def plan_theme_queries(plan: Any) -> list[Any]:
    """Theme queries from an LLM plan, tolerating the key names models tend to use."""
    if not isinstance(plan, dict):
        return []
    for key in ("theme_queries", "themeQueries", "scryfall_queries", "queries", "searches"):
        value = plan.get(key)
        if isinstance(value, list) and value:
            return value
    return []


def fallback_theme_queries(concept: str) -> list[str]:
    """Rules-text searches built straight from the user's concept, when the plan gave none.

    "proliferate experience counters" -> ['o:"experience counter"', 'o:proliferate'].
    Crude, but a theme search that finds nothing is harmless, while no theme search at
    all silently drops the concept from the card pool.
    """
    queries: list[str] = []
    padded = " " + (concept or "").lower().replace(",", " , ") + " "
    for phrase in re.split("[,.;&!?]| and | with | plus ", padded):
        words = [w for w in re.findall(r"[a-z0-9+/'-]+", phrase) if w not in _CONCEPT_STOPWORDS]
        words = [w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in words]
        if not words:
            continue
        queries.append(f'o:"{" ".join(words[-2:])}"' if len(words) > 1 else f"o:{words[0]}")
        if len(words) > 2:
            queries.append(f"o:{words[0]}")
    return sanitize_theme_queries(queries)


def _identity_filter(identity: Iterable[str]) -> str:
    colors = "".join(c for c in "WUBRG" if c in set(identity)).lower()
    return f"id<={colors or 'c'} f:commander"


def _is_valid(
    data: dict[str, Any],
    identity: set[str],
    banned: set[str],
    command_zone_names: set[str],
) -> Optional[str]:
    """Return a rejection reason, or None when the card may go in the pool."""
    name = (data.get("name") or "").lower()
    if not name or name in command_zone_names:
        return "commander"
    if (data.get("legalities") or {}).get("commander") != "legal":
        return "not legal"
    if not set(data.get("color_identity") or []) <= identity:
        return "off-color"
    if name in banned:
        return "banned list"
    if "Basic Land" in (data.get("type_line") or ""):
        return "basic"  # basics are added by the assembler, not picked
    return None


def _base_searches(identity: set[str], since: str) -> list[tuple[Optional[str], Optional[str], str, str, str, int]]:
    """Searches that depend only on the color identity (role staples, newest cards)."""
    base = _identity_filter(identity)
    searches: list[tuple[Optional[str], Optional[str], str, str, str, int]] = []
    for role, (fragment, limit) in ROLE_SEARCHES.items():
        searches.append((role, None, f"{fragment} {base}", "edhrec", "auto", limit))
    searches.append(
        ("synergy", "new", f"{base} -t:land new:card date>={since}", "released", "desc", GENERAL_NEW_LIMIT)
    )
    return searches


def _since(today: Optional[date]) -> str:
    return ((today or date.today()) - timedelta(days=NEW_CARD_WINDOW_DAYS)).isoformat()


async def prefetch_base_searches(
    scryfall: Any, command_zone: list[dict[str, Any]], today: Optional[date] = None
) -> None:
    """Warm the Scryfall search cache with the plan-independent searches.

    Run alongside the LLM planning call so these requests (rate-limited to ~2/s) are
    already done when the pool is built.
    """
    identity: set[str] = set()
    for commander in command_zone:
        identity.update(commander.get("color_identity") or [])
    for _, _, query, order, direction, limit in _base_searches(identity, _since(today)):
        try:
            await scryfall.search(query, order=order, direction=direction, limit=limit)
        except (ScryfallError, AttributeError) as exc:
            logger.warning("Card pool prefetch failed (%s): %s", query, exc)


async def build_card_pool(
    scryfall: Any,
    command_zone: list[dict[str, Any]],
    recommendations: dict[str, list[dict[str, Any]]],
    theme_queries: list[str],
    banned: set[str],
    gamechangers: set[str],
    gamechanger_limit: Optional[int],
    today: Optional[date] = None,
) -> CardPool:
    """Gather and validate candidates from EDHREC and Scryfall searches."""
    identity: set[str] = set()
    for commander in command_zone:
        identity.update(commander.get("color_identity") or [])
    command_zone_names = {(c.get("name") or "").lower() for c in command_zone}
    base = _identity_filter(identity)
    since = _since(today)

    # (role, tag, query, order, direction, limit)
    searches = _base_searches(identity, since)
    for query in theme_queries:
        searches.append(("theme", None, f"({query}) {base}", "edhrec", "auto", THEME_POPULAR_LIMIT))
        searches.append(
            ("theme", "new", f"({query}) {base} new:card date>={since}", "released", "desc", THEME_NEW_LIMIT)
        )

    async def run(search):
        role, tag, query, order, direction, limit = search
        try:
            return search, await scryfall.search(query, order=order, direction=direction, limit=limit)
        except (ScryfallError, AttributeError) as exc:
            logger.warning("Card pool search failed (%s): %s", query, exc)
            return search, []

    edhrec_entries = [
        (header, entry)
        for header, entries in (recommendations or {}).items()
        for entry in entries
        if entry.get("name")
    ]

    async def edhrec_lookup():
        names = list(dict.fromkeys(entry["name"] for _, entry in edhrec_entries))
        if not names:
            return []
        try:
            return await scryfall.get_collection(names)
        except ScryfallError as exc:
            logger.warning("Card pool EDHREC lookup failed: %s", exc)
            return []

    search_results, edhrec_cards = await asyncio.gather(
        asyncio.gather(*(run(s) for s in searches)), edhrec_lookup()
    )

    pool = CardPool()
    gc_names = {g.lower() for g in gamechangers}

    def consider(data: dict[str, Any], role: Optional[str], tag: Optional[str]) -> Optional[PoolCard]:
        reason = _is_valid(data, identity, banned, command_zone_names)
        if reason:
            pool.rejected[reason] += 1
            return None
        is_gc = (data.get("name") or "").lower() in gc_names
        if is_gc and gamechanger_limit == 0:
            pool.rejected["game changer"] += 1
            return None
        card = pool.add(data, role, tag)
        if card is not None and is_gc:
            card.tags.add("game changer")
        if card is not None and card.is_land:
            card.roles.add("land")
        return card

    # EDHREC first: it is the most commander-relevant source, so its cards get the
    # lowest numbers and the play counts used for ranking.
    by_name = {(c.get("name") or "").lower(): c for c in edhrec_cards if isinstance(c, dict)}
    for header, entry in edhrec_entries:
        data = by_name.get(entry["name"].lower())
        if data is None:
            continue
        role = "land" if "land" in header.lower() else "synergy"
        card = consider(data, role, "edhrec")
        if card is None:
            continue
        card.num_decks = max(card.num_decks, int(entry.get("num_decks") or 0))
        potential = int(entry.get("potential_decks") or 0)
        synergy = float(entry.get("synergy") or 0)
        if potential and synergy >= 0.25 and card.num_decks / potential < 0.15:
            card.tags.add("gem")

    for (role, tag, *_), cards in search_results:
        for data in cards:
            consider(data, role, tag)

    for card in pool.cards:
        rank = card.data.get("edhrec_rank")
        if "theme" in card.roles and not card.num_decks and "new" not in card.tags and (
            rank is None or rank >= GEM_MIN_EDHREC_RANK
        ):
            card.tags.add("gem")

    logger.info(
        "Card pool: %d candidates (%s) from %d searches + %d EDHREC cards; rejected %s",
        len(pool),
        ", ".join(f"{label} {sum(1 for c in pool.cards if c.label == label)}" for _, label in ROLE_LABELS),
        len(searches),
        len(by_name),
        dict(pool.rejected),
    )
    return pool


def _needs_oracle_text(card: PoolCard) -> bool:
    """Include rules text only for cards the model may not know (saves prompt space)."""
    rank = card.data.get("edhrec_rank")
    return bool({"new", "gem"} & card.tags) or rank is None or rank > 3000


def _card_line(card: PoolCard, include_prices: bool = False, numbered: bool = True) -> str:
    """One compact prompt line: '#12 Name {cost} | type $price [tags] :: rules text'."""
    cost = card.data.get("mana_cost") or ""
    if not cost and card.data.get("card_faces"):
        cost = (card.data["card_faces"][0] or {}).get("mana_cost") or ""
    tags = sorted(card.tags - {"edhrec"})
    extra = f" [{', '.join(tags)}]" if tags else ""
    price = ""
    if include_prices:
        usd = (card.data.get("prices") or {}).get("usd")
        price = f" ${usd}" if usd else ""
    prefix = f"#{card.id} " if numbered else ""
    line = f"{prefix}{card.name} {cost} | {card.type_line}{price}{extra}"
    if _needs_oracle_text(card):
        text = " ".join((card.data.get("oracle_text") or "").split())
        if not text and card.data.get("card_faces"):
            text = " // ".join(
                " ".join((f.get("oracle_text") or "").split()) for f in card.data["card_faces"]
            )
        if text:
            line += f" :: {text[:160]}"
    return line


def render_pool(pool: CardPool, include_prices: bool = False) -> str:
    """Render the pool as compact numbered lines grouped by category."""
    groups: dict[str, list[PoolCard]] = {}
    for card in pool.cards:
        groups.setdefault(card.label, []).append(card)
    lines: list[str] = []
    for _, label in ROLE_LABELS:
        cards = sorted(groups.get(label, []), key=lambda c: c.score(), reverse=True)
        if not cards:
            continue
        lines.append(f"## {label}")
        lines.extend(_card_line(card, include_prices) for card in cards)
    return "\n".join(lines)


def render_theme_candidates(pool: CardPool, exclude: set[str], limit: int = 60) -> str:
    """On-theme cards for a revision prompt (by name), skipping cards already in the deck.

    New releases and gems are listed first so they aren't crowded out by staples.
    """
    excluded = {name.lower() for name in exclude}
    cards = [c for c in pool.cards if "theme" in c.roles and not c.is_land and c.key not in excluded]
    cards.sort(key=lambda c: (bool({"new", "gem"} & c.tags), c.score()), reverse=True)
    return "\n".join(_card_line(card, numbered=False) for card in cards[:limit])


def clamp_targets(raw: Any, deck_size: int) -> dict[str, int]:
    """Turn the LLM plan's role counts into safe, deck-size-consistent targets."""
    raw = raw if isinstance(raw, dict) else {}

    def number(key: str, low: int, high: int) -> int:
        try:
            value = int(raw.get(key, DEFAULT_TARGETS[key]))
        except (TypeError, ValueError):
            value = DEFAULT_TARGETS[key]
        return max(low, min(high, value))

    targets = {"lands": number("lands", 35, 40)}
    for role in FILL_ROLES:
        targets[role] = number(role, 0, 15)
    nonland = deck_size - targets["lands"]
    fixed = sum(targets[r] for r in FILL_ROLES)
    if fixed > nonland - 15:  # always leave room for the deck's actual theme
        scale = (nonland - 15) / fixed
        for role in FILL_ROLES:
            targets[role] = int(targets[role] * scale)
    targets["theme"] = nonland - sum(targets[r] for r in FILL_ROLES)
    return targets


def _nonbasic_land_allowance(identity: set[str], land_target: int) -> int:
    """How many nonbasic lands to auto-add when the LLM picked none."""
    by_colors = {0: 4, 1: 4, 2: 9, 3: 13, 4: 16, 5: 18}
    return min(by_colors[len(identity & set("WUBRG"))], land_target)


def flatten_picks(picks: Any, targets: dict[str, int]) -> list[Any]:
    """Turn grouped picks ({"ramp": [...], "theme": [...], ...}) into one priority order.

    Each role's list is taken up to its target first, then the theme list, then any
    extra role picks - so trimming an over-long answer cuts the least important cards.
    A plain list is returned unchanged.
    """
    if isinstance(picks, list):
        return picks
    if not isinstance(picks, dict):
        return []
    groups = {str(k).lower(): v for k, v in picks.items() if isinstance(v, list)}
    ordered: list[Any] = []
    extras: list[Any] = []
    for role in FILL_ROLES:
        group = groups.get(role, [])
        ordered.extend(group[: targets.get(role, 0)])
        extras.extend(group[targets.get(role, 0):])
    ordered.extend(groups.get("theme", []))
    ordered.extend(extras)
    ordered.extend(groups.get("lands", []) + groups.get("land", []))
    for key, group in groups.items():  # any group name we didn't ask for
        if key not in (*FILL_ROLES, "theme", "lands", "land"):
            ordered.extend(group)
    return ordered


def assemble_deck(
    pool: CardPool,
    picks: Any,
    targets: dict[str, int],
    deck_size: int,
    identity: set[str],
    gamechanger_limit: Optional[int],
    notes: list[str],
) -> list[MagicCard]:
    """Build an exact-size deck from LLM picks (pool numbers), filling gaps from the pool."""
    land_target = targets["lands"]
    nonland_target = deck_size - land_target
    chosen: list[PoolCard] = []
    seen: set[str] = set()
    gc_count = 0
    skipped_gc = 0

    def take(card: PoolCard, picked: bool = False) -> bool:
        nonlocal gc_count, skipped_gc
        if card.key in seen:
            return False
        if card.is_game_changer and gamechanger_limit is not None and gc_count >= gamechanger_limit:
            skipped_gc += picked  # only report the model's own over-picks
            return False
        seen.add(card.key)
        gc_count += card.is_game_changer
        chosen.append(card)
        return True

    picks = flatten_picks(picks, targets)
    unknown = [p for p in picks if pool.get(p) is None]
    for ref in picks:
        card = pool.get(ref)
        if card is not None:
            take(card, picked=True)
    if unknown:
        notes.append(f"Ignored {len(unknown)} pick(s) that were not in the candidate pool.")

    nonlands = [c for c in chosen if not c.is_land]
    lands = [c for c in chosen if c.is_land]
    if len(nonlands) > nonland_target:
        kept = _keep_with_roles(nonlands, targets, nonland_target)
        cut = [c for c in nonlands if c.key not in kept]
        nonlands = [c for c in nonlands if c.key in kept]
        for card in cut:
            seen.discard(card.key)
        notes.append(
            f"Picked {len(nonlands) + len(cut)} nonland cards for {nonland_target} slots; kept the "
            f"role cards and top theme picks, dropped: {', '.join(c.name for c in cut[:8])}"
            f"{'...' if len(cut) > 8 else ''}."
        )

    # Fill roles the picks left short, then the theme, from the best-ranked candidates.
    role_counts = Counter(role for card in nonlands for role in card.roles)
    filled: list[str] = []

    def fill_from(candidates: list[PoolCard], limit: Optional[int] = None) -> int:
        added = 0
        for card in candidates:
            if len(nonlands) >= nonland_target or (limit is not None and added >= limit):
                break
            if card.is_land or not take(card):
                continue
            nonlands.append(card)
            role_counts.update(card.roles)
            filled.append(card.name)
            added += 1
        return added

    for role in FILL_ROLES:
        shortfall = targets.get(role, 0) - role_counts[role]
        if shortfall > 0:
            fill_from(pool.ranked(role), shortfall)
    fill_from(pool.ranked("theme"))
    fill_from(pool.ranked("synergy"))
    fill_from(pool.ranked())
    if filled:
        notes.append(
            f"Added {len(filled)} card(s) from the candidate pool to fill out the deck's roles: "
            f"{', '.join(filled[:10])}{'...' if len(filled) > 10 else ''}."
        )

    # Lands: the LLM's nonbasic picks (or the best-ranked ones if it chose none),
    # leaving at least one basic per color for fetch effects; basics fill the rest.
    colors = identity & set("WUBRG")
    max_nonbasic = max(0, land_target - len(colors))
    if not lands:
        lands = [c for c in pool.ranked("land") if c.key not in seen][
            : min(_nonbasic_land_allowance(identity, land_target), max_nonbasic)
        ]
    lands = lands[:max_nonbasic]
    basics_needed = land_target - len(lands)
    shortfall = nonland_target - len(nonlands)
    if shortfall > 0:
        basics_needed += shortfall
        notes.append(f"The candidate pool ran short; added {shortfall} extra basic land(s).")
    if skipped_gc:
        notes.append(
            f"Skipped {skipped_gc} game-changer pick(s) beyond the bracket limit of {gamechanger_limit}."
        )

    deck = [_to_deck_card(card) for card in nonlands + lands]
    deck.extend(_even_basics(colors, basics_needed))
    return deck


def _keep_with_roles(picks: list[PoolCard], targets: dict[str, int], slots: int) -> set[str]:
    """Choose which of too many picks to keep, protecting each role's target count.

    LLMs tend to list theme cards first and ramp/removal last, so simply cutting from
    the end strips the deck's interaction. Instead, for each role take picks (in the
    model's order) until that role's target is met, then fill the remaining slots with
    the highest-priority remaining picks.
    """
    kept: list[PoolCard] = []
    counts: Counter = Counter()
    for role in FILL_ROLES:
        for card in picks:
            if counts[role] >= targets.get(role, 0) or len(kept) >= slots:
                break
            if role in card.roles and card not in kept:
                kept.append(card)
                counts.update(card.roles)
    # The model's own exploration picks (recent releases, deep cuts) come next.
    explore = [c for c in picks if {"new", "gem"} & c.tags and c not in kept]
    for card in explore[:EXPLORE_KEEP]:
        if len(kept) >= slots:
            break
        kept.append(card)
    for card in picks:
        if len(kept) >= slots:
            break
        if card not in kept:
            kept.append(card)
    return {card.key for card in kept}


def _to_deck_card(card: PoolCard) -> MagicCard:
    data = card.data
    return MagicCard(
        name=card.name,
        count=1,
        category=card.label,
        scryfall_id=data.get("id"),
        mana_cost=data.get("mana_cost"),
        produced_mana=data.get("produced_mana") or [],
        type_line=data.get("type_line"),
        oracle_text=data.get("oracle_text"),
        price=(data.get("prices") or {}).get("usd"),
        colors=data.get("colors") or [],
        color_identity=data.get("color_identity") or [],
    )


def _even_basics(colors: set[str], count: int) -> list[MagicCard]:
    """Placeholder basics split evenly; ``_balance_basic_lands`` sets the real split."""
    if count <= 0:
        return []
    names = [BASIC_BY_COLOR[c] for c in "WUBRG" if c in colors] or ["Wastes"]
    counts = Counter(names[i % len(names)] for i in range(count))
    return [MagicCard(name=name, count=n, category="Land") for name, n in counts.items()]
