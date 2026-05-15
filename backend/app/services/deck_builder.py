"""High-level deck building orchestration.

Combines the Scryfall, EDHREC and LLM services into a single async pipeline that
produces a validated 99-card commander deck. If no LLM is configured (or it
fails), we fall back to a heuristic that picks the top EDHREC recommendations
by category and pads the mana base with basic lands.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..constants import gamechanger_limit
from ..models.schemas import (
    BRACKET_DESCRIPTIONS,
    BuildDeckRequest,
    MagicCard,
    DeckResponse,
    RevampDeckRequest,
)
from .edhrec import EDHRecClient, EDHRecError
from .llm import LLMService
from .scryfall import ScryfallClient, ScryfallError

logger = logging.getLogger(__name__)

DECK_SIZE = 99  # cards in the 99, commander is tracked separately
_BASIC_BY_COLOR = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}
_BASIC_NAMES = {*_BASIC_BY_COLOR.values(), "Wastes"}


class DeckBuilderError(RuntimeError):
    pass


class DeckBuilder:
    def __init__(
        self,
        scryfall: ScryfallClient,
        edhrec: EDHRecClient,
        llm: LLMService,
    ) -> None:
        self._scryfall = scryfall
        self._edhrec = edhrec
        self._llm = llm

    async def build(self, request: BuildDeckRequest) -> DeckResponse:
        commander_card = await self._resolve_commander(request.commander)
        recommendations = await self._fetch_recommendations(commander_card["name"])
        bracket_desc = BRACKET_DESCRIPTIONS[request.bracket]
        limit = gamechanger_limit(request.bracket)
        has_prompt = bool((request.prompt or "").strip())
        banned_list = set(name.strip().lower() for name in request.banned_list or [])
        gamechangers = set(name.strip().lower() for name in request.gamechangers or [])

        llm_result: Optional[dict[str, Any]] = None
        if has_prompt:
            if not self._llm.enabled:
                raise DeckBuilderError(
                    "A deck concept was provided but no Gemini API key is configured. "
                    "Set GEMINI_API_KEY in backend/.env to enable LLM-driven builds, "
                    "or clear the deck concept to use a heuristic build instead."
                )
            logger.info(
                "Build request: commander=%s bracket=%s prompt_chars=%d",
                commander_card.get("name"),
                request.bracket,
                len(request.prompt or ""),
            )
            llm_result = await self._llm.build_deck(
                commander=commander_card,
                bracket=request.bracket,
                bracket_description=bracket_desc,
                user_prompt=request.prompt,
                recommendations=recommendations,
                gamechanger_limit=limit,
                gamechangers=gamechangers,
                banned_list=banned_list,
            )
            if llm_result is None:
                logger.warning(
                    "LLM build returned None for commander=%s bracket=%s; surfacing 400.",
                    commander_card.get("name"),
                    request.bracket,
                )
                raise DeckBuilderError(
                    "Gemini failed to produce a valid deck. Check backend logs and "
                    "retry, or clear the deck concept to use a heuristic build."
                )

        validation = await self.validate_decklist(
            llm_result.get("decklist") if llm_result else [], commander_card.get("color_identity", [])
        )

        invalid_cards = validation[1]
        if invalid_cards: 
            llm_result = await self._llm.build_deck(
                commander=commander_card,
                bracket=request.bracket,
                bracket_description=bracket_desc,
                user_prompt=request.prompt,
                recommendations=recommendations,
                gamechanger_limit=limit,
                gamechangers=gamechangers,
                banned_list=banned_list,
                color_identity=commander_card.get("color_identity", []),
            )

        decklist, explanation, source, notes = self._materialize(
            llm_result, commander_card, recommendations, request.bracket, request.gamechangers
        )
        return DeckResponse(
            version=1,
            commander=_to_card(commander_card),
            bracket=request.bracket,
            bracket_description=bracket_desc,
            prompt=request.prompt,
            decklist=decklist,
            explanation=explanation,
            notes=notes,
            source=source,
        )
    

    async def validate_decklist(self, llm_generated_list, commander_colors):
        # 1. Clean the list from the LLM
        names = [c.get("name") if isinstance(c, dict) else c for c in llm_generated_list]
        
        # 2. Batch names into chunks of 75 (Scryfall's limit)
        # Use your scryfall service to call POST /cards/collection
        # with {"identifiers": [{"name": n} for n in names]}
        all_card_data = await self._scryfall.get_collection(names)
        
        valid_cards = []
        invalid_cards = []
        commander_identity = set(commander_colors)

        for data in all_card_data:
            logger.debug("Validating card: %s", data)
            # Check Legality & Identity locally (No more API calls!)
            is_legal = data.get('legalities', {}).get('commander') == 'legal'
            card_identity = set(data.get('color_identity', []))
            is_color_valid = card_identity.issubset(commander_identity)

            if is_legal and is_color_valid:
                valid_cards.append(data)
            else:
                reason = "Banned" if not is_legal else "Color Mismatch"
                invalid_cards.append(f"{data.get('name')} ({reason})")

        return valid_cards, invalid_cards

    async def revamp(self, request: RevampDeckRequest) -> DeckResponse:
        commander_card = await self._resolve_commander(request.commander)
        bracket_desc = BRACKET_DESCRIPTIONS[request.bracket]
        limit = gamechanger_limit(request.bracket)
        previous_payload = [card.model_dump() for card in request.previous_decklist]
        banned_list = set(name.strip().lower() for name in request.banned_list or [])
        gamechangers = set(name.strip().lower() for name in request.gamechangers or [])
        color_identity = commander_card.get("color_identity", [])

        llm_result = await self._llm.revamp_deck(
            commander=commander_card,
            bracket=request.bracket,
            bracket_description=bracket_desc,
            user_prompt=request.prompt,
            previous_decklist=previous_payload,
            change_request=request.change_request,
            gamechanger_limit=limit,
            gamechangers=gamechangers,
            banned_list=banned_list,
            color_identity=color_identity,
        )
        if llm_result is None:
            # Without an LLM we cannot meaningfully revamp; rebuild instead.
            recommendations = await self._fetch_recommendations(commander_card["name"])
            decklist, explanation, source, notes = self._materialize(
                None, commander_card, recommendations, request.bracket
            )
            notes.insert(0, "LLM unavailable - regenerated deck heuristically instead of revamping.")
        else:
            decklist, explanation, source, notes = self._materialize(
                llm_result, commander_card, {}, request.bracket
            )

        validation = await self.validate_decklist(
            llm_result.get("decklist") if llm_result else [], commander_card.get("color_identity", [])
        )

        invalid_cards = validation[1]
        if invalid_cards: 
            llm_result = await self._llm.revamp_deck(
                commander=commander_card,
                bracket=request.bracket,
                bracket_description=bracket_desc,
                user_prompt=request.prompt,
                recommendations=recommendations,
                gamechanger_limit=limit,
                gamechangers=gamechangers,
                banned_list=banned_list,
                previous_decklist=previous_payload,
                change_request=request.change_request,
            )

        return DeckResponse(
            version=request.previous_version + 1,
            commander=_to_card(commander_card),
            bracket=request.bracket,
            bracket_description=bracket_desc,
            prompt=request.prompt,
            decklist=decklist,
            explanation=explanation,
            notes=notes,
            source=source,
        )

    async def _resolve_commander(self, name: str) -> dict[str, Any]:
        try:
            return await self._scryfall.resolve_commander(name)
        except ScryfallError as exc:
            raise DeckBuilderError(str(exc)) from exc

    async def _fetch_recommendations(self, commander_name: str) -> dict[str, list[dict[str, Any]]]:
        try:
            page = await self._edhrec.commander_page(commander_name)
        except EDHRecError as exc:
            logger.warning("EDHREC fetch failed for %s: %s", commander_name, exc)
            return {}
        return self._edhrec.extract_recommendations(page)

    def _materialize(
        self,
        llm_result: Optional[dict[str, Any]],
        commander_card: dict[str, Any],
        recommendations: dict[str, list[dict[str, Any]]],
        bracket: int,
        gamechangers: set[str],
    ) -> tuple[list[MagicCard], str, str, list[str]]:
        notes: list[str] = []
        if llm_result and isinstance(llm_result.get("decklist"), list):
            raw_list = llm_result["decklist"]
            explanation = (llm_result.get("explanation") or "").strip() or "(no explanation provided)"
            extra_notes = llm_result.get("notes") or []
            if isinstance(extra_notes, list):
                notes.extend(str(n) for n in extra_notes)
            source = "llm"
        else:
            raw_list = _heuristic_decklist(commander_card, recommendations, bracket, gamechangers)
            explanation = _heuristic_explanation(commander_card, recommendations)
            source = "heuristic"
            notes.append(
                "Built without an LLM - top EDHREC picks plus a basic-land base were used."
            )

        raw_list = _enforce_gamechanger_limit(raw_list, bracket, notes, gamechangers)
        decklist = _normalize_decklist(raw_list, commander_card, notes)
        return decklist, explanation, source, notes


def _to_card(card: dict[str, Any]) -> MagicCard:
    return MagicCard(
        name=card.get("name", ""),
        count=1,
        category="Commander",
        scryfall_id=card.get("id"),
        mana_cost=card.get("mana_cost"),
        type_line=card.get("type_line"),
        oracle_text=card.get("oracle_text"),
        colors=card.get("colors") or [],
        color_identity=card.get("color_identity") or [],
        image_uri=card.get("image_uris", {}).get("normal"),
    )


def _normalize_decklist(
    raw_list: list[Any],
    commander_card: dict[str, Any],
    notes: list[str],
) -> list[MagicCard]:
    commander_name = (commander_card.get("name") or "").lower()
    seen: dict[str, MagicCard] = {}
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name or name.lower() == commander_name:
            continue
        try:
            count = int(entry.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, count)
        category = (entry.get("category") or "").strip() or None
        key = name.lower()
        is_basic = name in _BASIC_NAMES
        if key in seen:
            if is_basic:
                seen[key].count += count
            # Non-basics already present: ignore duplicates (singleton rule).
            continue
        if not is_basic:
            count = 1
        seen[key] = MagicCard(name=name, count=count, category=category)

    total = sum(card.count for card in seen.values())
    if total > DECK_SIZE:
        notes.append(f"Trimmed {total - DECK_SIZE} extra cards to reach 99.")
        _trim_to_size(seen, DECK_SIZE)
    elif total < DECK_SIZE:
        notes.append(f"Padded {DECK_SIZE - total} basic lands to reach 99.")
        _pad_with_basics(seen, commander_card, DECK_SIZE - total)

    return list(seen.values())


def _trim_to_size(deck: dict[str, MagicCard], target: int) -> None:
    while sum(c.count for c in deck.values()) > target:
        basic_keys = [k for k, v in deck.items() if v.name in _BASIC_NAMES and v.count > 1]
        if basic_keys:
            deck[basic_keys[-1]].count -= 1
            continue
        # Pop the last inserted non-basic.
        for key in reversed(list(deck.keys())):
            if deck[key].name not in _BASIC_NAMES:
                del deck[key]
                break
        else:
            break


def _pad_with_basics(deck: dict[str, MagicCard], commander_card: dict[str, Any], needed: int) -> None:
    colors = commander_card.get("color_identity") or []
    basics = [_BASIC_BY_COLOR[c] for c in colors if c in _BASIC_BY_COLOR] or ["Wastes"]
    i = 0
    while needed > 0:
        basic = basics[i % len(basics)]
        key = basic.lower()
        if key in deck:
            deck[key].count += 1
        else:
            deck[key] = MagicCard(name=basic, count=1, category="Land")
        needed -= 1
        i += 1


def _enforce_gamechanger_limit(
    raw_list: list[Any], bracket: int, notes: list[str], gamechangers: set[str]
) -> list[Any]:
    limit = gamechanger_limit(bracket)
    if limit is None:
        return raw_list
    kept: list[Any] = []
    gc_count = 0
    removed = 0
    for entry in raw_list:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        name = (entry.get("name") or "").strip()
        if name in gamechangers:
            if gc_count >= limit:
                removed += 1
                continue
            gc_count += 1
        kept.append(entry)
    if removed > 0:
        notes.append(
            f"Removed {removed} game-changer card(s) to respect the bracket {bracket} "
            f"limit of {limit}."
        )
    return kept


def _heuristic_decklist(
    commander_card: dict[str, Any],
    recommendations: dict[str, list[dict[str, Any]]],
    bracket: int,
    gamechangers: set[str],
) -> list[dict[str, Any]]:
    picks: list[dict[str, Any]] = []
    seen: set[str] = set()
    commander_name = (commander_card.get("name") or "").lower()
    limit = gamechanger_limit(bracket)
    gc_count = 0
    # Walk categories and grab their top recommendations until we approach 65 nonland picks.
    target_nonland = 65
    for header, cards in recommendations.items():
        for card in cards:
            name = card.get("name")
            if not name or name.lower() == commander_name or name.lower() in seen:
                continue
            if name in gamechangers and limit is not None and gc_count >= limit:
                continue
            picks.append({"name": name, "count": 1, "category": header})
            seen.add(name.lower())
            if name in gamechangers:
                gc_count += 1
            if len(picks) >= target_nonland:
                break
        if len(picks) >= target_nonland:
            break
    return picks


def _heuristic_explanation(
    commander_card: dict[str, Any],
    recommendations: dict[str, list[dict[str, Any]]],
) -> str:
    name = commander_card.get("name") or "the commander"
    categories = ", ".join(list(recommendations.keys())[:6]) or "no EDHREC categories"
    return (
        f"This is a heuristic build for {name}. It seeds the deck with the most-played "
        f"EDHREC cards across categories ({categories}) and pads the mana base with "
        f"basic lands matching the commander's color identity. Configure a Gemini API "
        f"key to enable LLM-guided card selection and explanations."
    )
