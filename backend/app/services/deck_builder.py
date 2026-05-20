"""High-level deck building orchestration.

Combines the Scryfall, EDHREC and LLM services into a single async pipeline that
produces a validated 99-card commander deck. If no LLM is configured (or it
fails), we fall back to a heuristic that picks the top EDHREC recommendations
by category and pads the mana base with basic lands.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..constants import GAMECHANGERS, gamechanger_limit
from ..models.schemas import (
    BRACKET_DESCRIPTIONS,
    BuildDeckRequest,
    DeckSubstitution,
    MagicCard,
    MagicCardFace,
    DeckResponse,
    RevampDeckRequest,
    ScryfallImageUris,
)
from .edhrec import EDHRecClient, EDHRecError
from .llm import LLMService
from .scryfall import ScryfallClient, ScryfallError

logger = logging.getLogger(__name__)

DECK_SIZE = 99  # cards in the 99, commander is tracked separately
DECK_SIZE_WITH_PARTNER = 98  # max non-commander cards when a partner is present
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
        """Store the service dependencies used by the deck-building pipeline."""
        self._scryfall = scryfall
        self._edhrec = edhrec
        self._llm = llm

    async def build(self, request: BuildDeckRequest) -> DeckResponse:
        """Build a new deck from a commander, optionally using Gemini for card selection."""
        commander_card = await self._resolve_commander(request.commander)
        secondary_commander_card = await self._resolve_secondary_commander(
            commander_card,
            request.secondary_commander,
        )
        command_zone = self._combine_command_zone(commander_card, secondary_commander_card)
        recommendations = await self._fetch_recommendations(commander_card["name"])
        bracket_desc = BRACKET_DESCRIPTIONS[request.bracket]
        limit = gamechanger_limit(request.bracket)
        has_prompt = bool((request.prompt or "").strip())
        banned_list = set(name.strip().lower() for name in request.banned_list or [])
        gamechangers = set(name.strip().lower() for name in request.gamechangers or [])

        llm_result: Optional[dict[str, Any]] = None
        prefetched_cards: dict[str, dict[str, Any]] = {}
        if has_prompt:
            if not self._llm.enabled:
                raise DeckBuilderError(
                    "A deck concept was provided but no Gemini API key is configured. "
                    "Set GEMINI_API_KEY in backend/.env to enable LLM-driven builds, "
                    "or clear the deck concept to use a heuristic build instead."
                )
            logger.info(
                "Build request: commander=%s bracket=%s prompt_chars=%d",
                command_zone.get("name"),
                request.bracket,
                len(request.prompt or ""),
            )
            llm_result = await self._llm.build_deck(
                commander=command_zone,
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
                    command_zone.get("name"),
                    request.bracket,
                )
                raise DeckBuilderError(
                    "Gemini failed to produce a valid deck. Check backend logs and "
                    "retry, or clear the deck concept to use a heuristic build."
                )

        if llm_result:
            prefetched_cards, invalid_cards = await self.validate_decklist(
                llm_result.get("decklist") or [], command_zone.get("color_identity", [])
            )
        else:
            invalid_cards = []
        if invalid_cards: 
            llm_result = await self._llm.build_deck(
                commander=command_zone,
                bracket=request.bracket,
                bracket_description=bracket_desc,
                user_prompt=request.prompt,
                recommendations=recommendations,
                gamechanger_limit=limit,
                gamechangers=gamechangers,
                banned_list=banned_list,
            )
            prefetched_cards = {}

        decklist, explanation, source, notes, substitutions = self._materialize(
            llm_result,
            command_zone,
            recommendations,
            request.bracket,
            gamechangers,
            secondary_commander_card,
        )
        decklist = await self._hydrate_cards(decklist, prefetched_cards)
        substitutions = await self._hydrate_substitutions(substitutions, prefetched_cards)
        return DeckResponse(
            version=1,
            commander=_to_card(commander_card),
            secondary_commander=_to_card(secondary_commander_card) if secondary_commander_card else None,
            bracket=request.bracket,
            bracket_description=bracket_desc,
            prompt=request.prompt,
            decklist=decklist,
            explanation=explanation,
            notes=notes,
            substitutions=substitutions,
            source=source,
        )
    

    async def validate_decklist(self, llm_generated_list, commander_colors):
        """Validate LLM-proposed cards for Commander legality and color identity."""
        names = [str(c.get("name") if isinstance(c, dict) else c).strip() for c in llm_generated_list if (c.get("name") if isinstance(c, dict) else c)]
        all_card_data = await self._scryfall.get_collection(names)

        resolved_cards: dict[str, dict[str, Any]] = {}
        for requested_name in names:
            requested_key = requested_name.lower()
            matched_card = next(
                (card for card in all_card_data if _card_matches_requested_name(card, requested_name)),
                None,
            )
            if matched_card is not None:
                resolved_cards[requested_key] = matched_card

        valid_cards: dict[str, dict[str, Any]] = {}
        invalid_cards: list[str] = []
        commander_identity = set(commander_colors)

        for requested_name in names:
            requested_key = requested_name.lower()
            data = resolved_cards.get(requested_key)
            if data is None:
                data = await self._scryfall.get_card(requested_name)
                if data is None:
                    invalid_cards.append(f"{requested_name} (Not found)")
                    continue

            is_legal = data.get('legalities', {}).get('commander') == 'legal'
            card_identity = set(data.get('color_identity', []))
            is_color_valid = card_identity.issubset(commander_identity)

            if is_legal and is_color_valid:
                valid_cards[requested_key] = data
                actual_name = (data.get('name') or '').strip().lower()
                if actual_name:
                    valid_cards[actual_name] = data
            else:
                reason = "Banned" if not is_legal else "Color Mismatch"
                invalid_cards.append(f"{requested_name} ({reason})")

        return valid_cards, invalid_cards

    async def revamp(self, request: RevampDeckRequest) -> DeckResponse:
        """Revise an existing decklist and return the next version of the deck."""
        commander_card = await self._resolve_commander(request.commander)
        secondary_commander_card = await self._resolve_secondary_commander(
            commander_card,
            request.secondary_commander,
        )
        command_zone = self._combine_command_zone(commander_card, secondary_commander_card)
        bracket_desc = BRACKET_DESCRIPTIONS[request.bracket]
        limit = gamechanger_limit(request.bracket)
        previous_payload = [card.model_dump() for card in request.previous_decklist]
        banned_list = set(name.strip().lower() for name in request.banned_list or [])
        gamechangers = set(name.strip().lower() for name in request.gamechangers or [])
        recommendations: dict[str, list[dict[str, Any]]] = {}
        prefetched_cards: dict[str, dict[str, Any]] = {}

        llm_result = await self._llm.revamp_deck(
            commander=command_zone,
            bracket=request.bracket,
            bracket_description=bracket_desc,
            user_prompt=request.prompt,
            previous_decklist=previous_payload,
            change_request=request.change_request,
            gamechanger_limit=limit,
            gamechangers=gamechangers,
            banned_list=banned_list,
        )
        if llm_result is None:
            # Without an LLM we cannot meaningfully revamp; rebuild instead.
            recommendations = await self._fetch_recommendations(commander_card["name"])
            decklist, explanation, source, notes, substitutions = self._materialize(
                None,
                command_zone,
                recommendations,
                request.bracket,
                gamechangers,
                secondary_commander_card,
            )
            notes.insert(0, "LLM unavailable - regenerated deck heuristically instead of revamping.")
        else:
            prefetched_cards, invalid_cards = await self.validate_decklist(
                llm_result.get("decklist") or [], command_zone.get("color_identity", [])
            )
            decklist, explanation, source, notes, substitutions = self._materialize(
                llm_result, command_zone, {}, request.bracket, gamechangers, secondary_commander_card
            )
        decklist = await self._hydrate_cards(decklist, prefetched_cards)
        substitutions = await self._hydrate_substitutions(substitutions, prefetched_cards)

        return DeckResponse(
            version=request.previous_version + 1,
            commander=_to_card(commander_card),
            secondary_commander=_to_card(secondary_commander_card) if secondary_commander_card else None,
            bracket=request.bracket,
            bracket_description=bracket_desc,
            prompt=request.prompt,
            decklist=decklist,
            explanation=explanation,
            notes=notes,
            substitutions=substitutions,
            source=source,
        )

    async def _resolve_commander(self, name: str) -> dict[str, Any]:
        """Resolve a commander name through Scryfall and surface user-safe errors."""
        try:
            return await self._scryfall.resolve_commander(name)
        except ScryfallError as exc:
            raise DeckBuilderError(str(exc)) from exc

    async def _resolve_secondary_commander(
        self,
        commander_card: dict[str, Any],
        secondary_name: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Resolve and validate an optional second commander/background selection."""
        if not secondary_name or not secondary_name.strip():
            return None
        options_payload = await self._scryfall.get_commander_companion_options(commander_card.get("name", ""))
        allowed_names = options_payload.get("options") or []
        if not allowed_names:
            raise DeckBuilderError(
                f"{commander_card.get('name')} does not support a secondary commander or background."
            )
        secondary_card = await self._scryfall.get_card(secondary_name)
        if secondary_card is None:
            raise DeckBuilderError(f"Could not resolve secondary commander/background {secondary_name!r}.")
        if not any(_card_matches_requested_name(secondary_card, option_name) for option_name in allowed_names):
            relationship = options_payload.get("relationship") or "secondary command-zone option"
            raise DeckBuilderError(
                f"{secondary_card.get('name', secondary_name)!r} is not a valid {relationship.lower()} for "
                f"{commander_card.get('name')!r}."
            )
        return secondary_card

    def _combine_command_zone(
        self,
        commander_card: dict[str, Any],
        secondary_commander_card: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Merge one or two command-zone cards into a single prompt/validation context."""
        if secondary_commander_card is None:
            return commander_card
        ordered_colors = [
            color
            for color in ["W", "U", "B", "R", "G"]
            if color in set(commander_card.get("color_identity") or []) | set(secondary_commander_card.get("color_identity") or [])
        ]
        return {
            "name": f"{commander_card.get('name')} + {secondary_commander_card.get('name')}",
            "type_line": f"{commander_card.get('type_line') or '(unknown)'} / {secondary_commander_card.get('type_line') or '(unknown)'}",
            "oracle_text": (
                f"{commander_card.get('name')}: {commander_card.get('oracle_text') or '(none)'}\n\n"
                f"{secondary_commander_card.get('name')}: {secondary_commander_card.get('oracle_text') or '(none)'}"
            ),
            "color_identity": ordered_colors,
        }

    async def _fetch_recommendations(self, commander_name: str) -> dict[str, list[dict[str, Any]]]:
        """Fetch EDHREC category recommendations for a commander."""
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
        gamechangers: Optional[set[str]] = None,
        secondary_commander_card: Optional[dict[str, Any]] = None,
    ) -> tuple[list[MagicCard], str, str, list[str], list[DeckSubstitution]]:
        """Turn raw LLM or heuristic output into normalized cards plus metadata."""
        notes: list[str] = []
        substitutions: list[DeckSubstitution] = []
        if llm_result and isinstance(llm_result.get("decklist"), list):
            raw_list = llm_result["decklist"]
            explanation = (llm_result.get("explanation") or "").strip() or "(no explanation provided)"
            extra_notes = llm_result.get("notes") or []
            if isinstance(extra_notes, list):
                notes.extend(str(n) for n in extra_notes)
            substitutions = _normalize_substitutions(llm_result.get("substitutions"))
            source = "llm"
        else:
            raw_list = _heuristic_decklist(
                commander_card,
                recommendations,
                bracket,
                gamechangers,
                secondary_commander_card,
            )
            explanation = _heuristic_explanation(commander_card, recommendations)
            source = "heuristic"
            notes.append(
                "Built without an LLM - top EDHREC picks plus a basic-land base were used."
            )

        raw_list = _enforce_gamechanger_limit(raw_list, bracket, notes, gamechangers)
        decklist = _normalize_decklist(raw_list, commander_card, notes, secondary_commander_card)
        return decklist, explanation, source, notes, substitutions

    async def _hydrate_cards(
        self,
        decklist: list[MagicCard],
        prefetched_cards: Optional[dict[str, dict[str, Any]]] = None,
    ) -> list[MagicCard]:
        """Attach Scryfall metadata like images, oracle text, and price to deck cards."""
        if not decklist:
            return decklist
        cards_by_name = dict(prefetched_cards or {})
        missing_names = [card.name for card in decklist if card.name.lower() not in cards_by_name]
        if missing_names:
            try:
                collection = await self._scryfall.get_collection(missing_names)
            except ScryfallError as exc:
                logger.warning("Unable to hydrate decklist card metadata from Scryfall: %s", exc)
                collection = []
        else:
            collection = []

        cards_by_name.update(
            {
                (card.get("name") or "").lower(): card
                for card in collection
                if isinstance(card, dict) and card.get("name")
            }
        )
        for missing_name in missing_names:
            if missing_name.lower() in cards_by_name:
                continue
            fuzzy_card = await self._scryfall.get_card(missing_name)
            if fuzzy_card is None:
                continue
            cards_by_name[missing_name.lower()] = fuzzy_card
            actual_name = (fuzzy_card.get("name") or "").lower()
            if actual_name:
                cards_by_name[actual_name] = fuzzy_card
        for deck_card in decklist:
            data = cards_by_name.get(deck_card.name.lower())
            if not data:
                continue
            deck_card.scryfall_id = data.get("id") or deck_card.scryfall_id
            deck_card.mana_cost = data.get("mana_cost") or deck_card.mana_cost
            deck_card.produced_mana = data.get("produced_mana") or deck_card.produced_mana
            deck_card.type_line = data.get("type_line") or deck_card.type_line
            deck_card.oracle_text = data.get("oracle_text") or deck_card.oracle_text
            deck_card.price = data.get("prices", {}).get("usd") or deck_card.price
            deck_card.colors = data.get("colors") or deck_card.colors
            deck_card.color_identity = data.get("color_identity") or deck_card.color_identity
            deck_card.image_uris = _to_image_uris(data.get("image_uris")) or deck_card.image_uris
            deck_card.card_faces = _to_card_faces(data.get("card_faces")) or deck_card.card_faces
        return decklist

    async def _hydrate_substitutions(
        self,
        substitutions: list[DeckSubstitution],
        prefetched_cards: Optional[dict[str, dict[str, Any]]] = None,
    ) -> list[DeckSubstitution]:
        """Attach card metadata to the cards used in substitution explanations."""
        swap_cards = [card for substitution in substitutions for card in (*substitution.removed, *substitution.added)]
        await self._hydrate_cards(swap_cards, prefetched_cards)
        return substitutions


def _to_card(card: dict[str, Any]) -> MagicCard:
    """Convert a raw Scryfall commander payload into the API's ``MagicCard`` model."""
    return MagicCard(
        name=card.get("name", ""),
        count=1,
        category="Commander",
        scryfall_id=card.get("id"),
        mana_cost=card.get("mana_cost"),
        produced_mana=card.get("produced_mana") or [],
        type_line=card.get("type_line"),
        oracle_text=card.get("oracle_text"),
        price=card.get("prices", {}).get("usd"),
        colors=card.get("colors") or [],
        color_identity=card.get("color_identity") or [],
        image_uris=_to_image_uris(card.get("image_uris")),
        card_faces=_to_card_faces(card.get("card_faces")),
    )


def _to_image_uris(image_uris: Any) -> Optional[ScryfallImageUris]:
    """Validate a raw ``image_uris`` mapping into the typed schema model."""
    if not isinstance(image_uris, dict):
        return None
    return ScryfallImageUris.model_validate(image_uris)


def _to_card_faces(card_faces: Any) -> list[MagicCardFace]:
    """Validate a raw ``card_faces`` list into typed face models."""
    if not isinstance(card_faces, list):
        return []
    return [MagicCardFace.model_validate(face) for face in card_faces if isinstance(face, dict)]


def _normalize_substitutions(raw_substitutions: Any) -> list[DeckSubstitution]:
    """Normalize LLM substitution payloads into typed substitution objects."""
    if not isinstance(raw_substitutions, list):
        return []
    substitutions: list[DeckSubstitution] = []
    for entry in raw_substitutions:
        if not isinstance(entry, dict):
            continue
        removed = _normalize_swap_cards(entry.get("removed"))
        added = _normalize_swap_cards(entry.get("added"))
        explanation = str(entry.get("explanation") or entry.get("reason") or "").strip()
        if not removed and not added:
            continue
        substitutions.append(
            DeckSubstitution(
                removed=removed,
                added=added,
                explanation=explanation or "(no substitution explanation provided)",
            )
        )
    return substitutions


def _normalize_swap_cards(raw_cards: Any) -> list[MagicCard]:
    """Normalize the added/removed card lists inside an LLM substitution entry."""
    if isinstance(raw_cards, str):
        raw_cards = [{"name": raw_cards, "count": 1}]
    if not isinstance(raw_cards, list):
        return []

    normalized: list[MagicCard] = []
    for entry in raw_cards:
        if isinstance(entry, str):
            name = entry.strip()
            count = 1
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            try:
                count = int(entry.get("count") or 1)
            except (TypeError, ValueError):
                count = 1
        else:
            continue
        if not name:
            continue
        normalized.append(MagicCard(name=name, count=max(1, count)))
    return normalized


def _normalize_decklist(
    raw_list: list[Any],
    commander_card: dict[str, Any],
    notes: list[str],
    secondary_commander_card: Optional[dict[str, Any]] = None,
) -> list[MagicCard]:
    """Enforce singleton rules and size constraints on a raw 99-card decklist."""
    commander_names = {
        (commander_card.get("name") or "").lower(),
        (secondary_commander_card or {}).get("name", "").lower(),
    }
    total_cards_for_deck = DECK_SIZE_WITH_PARTNER if secondary_commander_card else DECK_SIZE
    seen: dict[str, MagicCard] = {}
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name or name.lower() in commander_names:
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
    if total > total_cards_for_deck:
        notes.append(f"Trimmed {total - total_cards_for_deck} extra cards to reach {total_cards_for_deck}.")
        _trim_to_size(seen, total_cards_for_deck)
    elif total < total_cards_for_deck:
        notes.append(f"Padded {total_cards_for_deck - total} basic lands to reach {total_cards_for_deck}.")
        _pad_with_basics(seen, commander_card, total_cards_for_deck - total)

    return list(seen.values())


def _trim_to_size(deck: dict[str, MagicCard], target: int) -> None:
    """Trim a normalized deck down to the target size, preferring basic lands first."""
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
    """Pad an undersized deck with basics matching the commander's color identity."""
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
    raw_list: list[Any], bracket: int, notes: list[str], gamechangers: Optional[set[str]] = None
) -> list[Any]:
    """Drop excess game-changers from a candidate list to satisfy bracket rules."""
    limit = gamechanger_limit(bracket)
    if limit is None:
        return raw_list
    source_names = gamechangers or {name.lower() for name in GAMECHANGERS}
    gamechanger_names = {name.strip().lower() for name in source_names if name}
    kept: list[Any] = []
    gc_count = 0
    removed = 0
    for entry in raw_list:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        name = (entry.get("name") or "").strip()
        if name.lower() in gamechanger_names:
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
    gamechangers: Optional[set[str]] = None,
    secondary_commander_card: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Create a simple candidate list from EDHREC recommendations plus bracket rules."""
    picks: list[dict[str, Any]] = []
    seen: set[str] = set()
    command_zone_names = {
        (commander_card.get("name") or "").lower(),
        (secondary_commander_card or {}).get("name", "").lower(),
    }
    limit = gamechanger_limit(bracket)
    gc_count = 0
    source_names = gamechangers or {name.lower() for name in GAMECHANGERS}
    gamechanger_names = {name.strip().lower() for name in source_names if name}
    # Walk categories and grab their top recommendations until we approach 65 nonland picks.
    target_nonland = 65
    for header, cards in recommendations.items():
        for card in cards:
            name = card.get("name")
            if not name or name.lower() in command_zone_names or name.lower() in seen:
                continue
            if name.lower() in gamechanger_names and limit is not None and gc_count >= limit:
                continue
            picks.append({"name": name, "count": 1, "category": header})
            seen.add(name.lower())
            if name.lower() in gamechanger_names:
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
    """Explain how a heuristic deck was assembled when no LLM result is available."""
    name = commander_card.get("name") or "the commander"
    categories = ", ".join(list(recommendations.keys())[:6]) or "no EDHREC categories"
    return (
        f"This is a heuristic build for {name}. It seeds the deck with the most-played "
        f"EDHREC cards across categories ({categories}) and pads the mana base with "
        f"basic lands matching the commander's color identity. Configure a Gemini API "
        f"key to enable LLM-guided card selection and explanations."
    )


def _card_reference_names(card: dict[str, Any]) -> set[str]:
    """Collect all useful names that can legitimately identify a Scryfall card."""
    names = {
        str(card.get("name") or "").strip().lower(),
        str(card.get("printed_name") or "").strip().lower(),
    }
    for face in card.get("card_faces") or []:
        if not isinstance(face, dict):
            continue
        names.add(str(face.get("name") or "").strip().lower())
        names.add(str(face.get("printed_name") or "").strip().lower())
    return {name for name in names if name}


def _card_matches_requested_name(card: dict[str, Any], requested_name: str) -> bool:
    """Check whether a requested name matches any canonical or alternate card name."""
    return requested_name.strip().lower() in _card_reference_names(card)
