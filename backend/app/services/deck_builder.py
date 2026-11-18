"""High-level deck building orchestration.

Combines the Scryfall, EDHREC and LLM services into a single async pipeline that
produces a validated 99-card commander deck. If no LLM is configured (or it
fails), we fall back to a heuristic that picks the top EDHREC recommendations
by category and pads the mana base with basic lands.
"""
from __future__ import annotations
from collections import Counter
import asyncio
import hashlib
import json
import re
import time

import logging
from typing import Any, Iterable, Optional

from ..config import get_settings
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
from .card_pool import (
    CardPool,
    assemble_deck,
    build_card_pool,
    clamp_targets,
    prefetch_base_searches,
    render_pool,
    sanitize_theme_queries,
)
from .llm import LLMService, _budget_block
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
        self._response_cache: dict[str, tuple[float, DeckResponse]] = {}
        self._response_cache_ttl = get_settings().response_cache_ttl_seconds

    def _cache_get(self, key: str) -> Optional[DeckResponse]:
        """Return a cached response for an identical prior request, if still fresh."""
        entry = self._response_cache.get(key)
        if entry is None:
            return None
        cached_at, response = entry
        if time.monotonic() - cached_at > self._response_cache_ttl:
            del self._response_cache[key]
            return None
        return response

    def _cache_set(self, key: str, response: DeckResponse) -> None:
        """Store a response for a short time so identical repeat requests are free."""
        if len(self._response_cache) > 500:
            self._response_cache.clear()
        self._response_cache[key] = (time.monotonic(), response)

    @staticmethod
    def _build_cache_key(request: BuildDeckRequest) -> str:
        """Hash the parts of a build request that fully determine its response."""
        payload = {
            "kind": "build",
            "commander": request.commander.strip().lower(),
            "secondary_commander": (request.secondary_commander or "").strip().lower(),
            "bracket": request.bracket,
            "prompt": (request.prompt or "").strip(),
            "gamechangers": sorted(name.strip().lower() for name in request.gamechangers or []),
            "banned_list": sorted(name.strip().lower() for name in request.banned_list or []),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _revamp_cache_key(request: RevampDeckRequest) -> str:
        """Hash the parts of a revamp request that fully determine its response."""
        payload = {
            "kind": "revamp",
            "commander": request.commander.strip().lower(),
            "secondary_commander": (request.secondary_commander or "").strip().lower(),
            "bracket": request.bracket,
            "prompt": (request.prompt or "").strip(),
            "gamechangers": sorted(name.strip().lower() for name in request.gamechangers or []),
            "banned_list": sorted(name.strip().lower() for name in request.banned_list or []),
            "previous_version": request.previous_version,
            "previous_decklist": sorted(
                (card.name.strip().lower(), int(card.count or 1)) for card in request.previous_decklist
            ),
            "change_request": (request.change_request or "").strip(),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    async def build(self, request: BuildDeckRequest) -> DeckResponse:
        """Build a new deck: plan (LLM), gather a validated card pool, pick (LLM), assemble.

        Without a deck concept the LLM is skipped and the deck is assembled from the
        same pool using default role targets, ranked by EDHREC data.
        """
        cache_key = self._build_cache_key(request)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        commander_card = await self._resolve_commander(request.commander)
        secondary_commander_card = await self._resolve_secondary_commander(
            commander_card,
            request.secondary_commander,
        )
        command_zone = self._combine_command_zone(commander_card, secondary_commander_card)
        command_cards = [c for c in (commander_card, secondary_commander_card) if c]
        identity = set(command_zone.get("color_identity") or [])
        deck_size = DECK_SIZE_WITH_PARTNER if secondary_commander_card else DECK_SIZE
        bracket_desc = BRACKET_DESCRIPTIONS[request.bracket]
        limit = gamechanger_limit(request.bracket)
        has_prompt = bool((request.prompt or "").strip())
        banned_list = set(name.strip().lower() for name in request.banned_list or [])
        gamechangers = set(name.strip().lower() for name in request.gamechangers or []) or {
            name.lower() for name in GAMECHANGERS
        }

        if has_prompt and not self._llm.enabled:
            raise DeckBuilderError(
                "A deck concept was provided but no LLM API key is configured. "
                "Set GEMINI_API_KEY (or LLM_PROVIDER=openai with OPENAI_API_KEY) in "
                "backend/.env to enable LLM-driven builds, "
                "or clear the deck concept to use a heuristic build instead."
            )

        # Step 1 (LLM): plan the strategy, role counts and theme searches. Runs
        # alongside the EDHREC fetch since neither depends on the other.
        plan: dict[str, Any] = {}
        if has_prompt:
            logger.info(
                "Build request: commander=%s bracket=%s prompt_chars=%d",
                command_zone.get("name"),
                request.bracket,
                len(request.prompt or ""),
            )
            plan_result, recommendations, _ = await asyncio.gather(
                self._llm.plan_deck(
                    commander=command_zone,
                    bracket=request.bracket,
                    bracket_description=bracket_desc,
                    user_prompt=request.prompt,
                    gamechanger_limit=limit,
                ),
                self._fetch_recommendations(commander_card["name"]),
                prefetch_base_searches(self._scryfall, command_cards),
            )
            if plan_result is None:
                raise DeckBuilderError(self._llm_failure_message("deck plan"))
            plan = plan_result
        else:
            recommendations = await self._fetch_recommendations(commander_card["name"])
        targets = clamp_targets(plan.get("targets"), deck_size)
        theme_queries = sanitize_theme_queries(plan.get("theme_queries"))

        # Step 2: a numbered pool of real, legal, on-color candidates.
        pool = await build_card_pool(
            self._scryfall,
            command_cards,
            recommendations,
            theme_queries,
            banned_list,
            gamechangers,
            limit,
        )

        # Step 3 (LLM): pick from the pool by number.
        notes: list[str] = []
        picks: list[Any] = []
        if has_prompt:
            selection = await self._llm.select_cards(
                commander=command_zone,
                bracket=request.bracket,
                bracket_description=bracket_desc,
                user_prompt=request.prompt,
                plan=plan,
                targets=targets,
                pool_text=render_pool(pool, include_prices=bool(_budget_block(request.prompt))),
                deck_size=deck_size,
                gamechanger_limit=limit,
            )
            if selection is None or not isinstance(selection.get("picks"), (list, dict)):
                raise DeckBuilderError(self._llm_failure_message("deck"))
            picks = selection["picks"]
            strategy = str(plan.get("strategy") or "").strip()
            explanation = str(selection.get("explanation") or "").strip() or strategy or "(no explanation provided)"
            extra_notes = selection.get("notes")
            if isinstance(extra_notes, list):
                notes.extend(str(n) for n in extra_notes if n)
            if theme_queries:
                notes.append(f"Searched for theme cards with: {'; '.join(theme_queries)}.")
            source = "llm"
        else:
            explanation = _heuristic_explanation(commander_card, recommendations)
            notes.append(
                "Built without an LLM - role staples and top EDHREC picks from the candidate pool."
            )
            source = "heuristic"

        # Step 4 (code): exact-size deck, roles filled, land count and colors balanced.
        decklist = assemble_deck(pool, picks, targets, deck_size, identity, limit, notes)
        decklist = await self._hydrate_cards(decklist, {card.key: card.data for card in pool.cards})
        decklist = await self._enforce_land_count(
            decklist, command_cards, recommendations, banned_list | gamechangers, notes
        )
        decklist = await self._balance_basics(decklist, command_cards, notes)
        notes.extend(_exploration_note(pool, decklist))
        response = DeckResponse(
            version=1,
            commander=_to_card(commander_card),
            secondary_commander=_to_card(secondary_commander_card) if secondary_commander_card else None,
            bracket=request.bracket,
            bracket_description=bracket_desc,
            prompt=request.prompt,
            decklist=decklist,
            explanation=explanation,
            notes=notes,
            substitutions=[],
            source=source,
        )
        self._cache_set(cache_key, response)
        return response


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
        cache_key = self._revamp_cache_key(request)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        commander_card = await self._resolve_commander(request.commander)
        secondary_commander_card = await self._resolve_secondary_commander(
            commander_card,
            request.secondary_commander,
        )
        command_zone = self._combine_command_zone(commander_card, secondary_commander_card)
        bracket_desc = BRACKET_DESCRIPTIONS[request.bracket]
        limit = gamechanger_limit(request.bracket)
        previous_payload = [card.model_dump() for card in request.previous_decklist]
        previous_normalized = _normalize_decklist(
            previous_payload,
            command_zone,
            [],
            secondary_commander_card,
        )
        banned_list = set(name.strip().lower() for name in request.banned_list or [])
        gamechangers = set(name.strip().lower() for name in request.gamechangers or [])
        recommendations: dict[str, list[dict[str, Any]]] = {}
        prefetched_cards: dict[str, dict[str, Any]] = {}
        llm_failed = False

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
            llm_failed = True
            reason = getattr(self._llm, "last_failure_reason", None)
            notes = [
                "LLM unavailable"
                + (f" ({reason})" if reason else "")
                + " - kept the current decklist instead of revamping."
            ]
            substitutions = []
            decklist = _normalize_decklist(
                previous_payload,
                command_zone,
                notes,
                secondary_commander_card,
            )
            explanation = "LLM failed to revamp the deck. The previous decklist was preserved unchanged."
            source = "heuristic"
        else:
            # Validate the new list AND every card a suggested swap adds - swaps used to
            # skip validation, so off-color suggestions reached the UI.
            swap_additions = [
                added
                for sub in llm_result.get("substitutions") or []
                if isinstance(sub, dict)
                for added in sub.get("added") or []
                if isinstance(added, dict)
            ]
            prefetched_cards, invalid_cards = await self.validate_decklist(
                list(llm_result.get("decklist") or []) + swap_additions,
                command_zone.get("color_identity", []),
            )
            if invalid_cards:
                llm_result = _drop_invalid_revamp_cards(
                    llm_result, prefetched_cards, invalid_cards, previous_payload
                )
            decklist, explanation, source, notes, substitutions = self._materialize(
                llm_result,
                command_zone,
                {},
                request.bracket,
                gamechangers,
                secondary_commander_card,
                prefetched_cards,
            )
        decklist = await self._hydrate_cards(decklist, prefetched_cards)
        if llm_result is not None:
            # Only adjust decks the LLM produced; a preserved (failed-revamp) deck is the
            # user's own list and is returned untouched. Cards the revamp just added are
            # protected when spells must become lands.
            command_cards = [c for c in (commander_card, secondary_commander_card) if c]
            just_added = {
                card.name for sub in substitutions for card in sub.added
            }
            decklist = await self._enforce_land_count(
                decklist,
                command_cards,
                {},
                banned_list | gamechangers,
                notes,
                protected=just_added,
            )
            decklist = await self._balance_basics(decklist, command_cards, notes)
        substitutions = await self._hydrate_substitutions(substitutions, prefetched_cards)
        next_version = (
            request.previous_version + 1
            if _decklist_signature(decklist) != _decklist_signature(previous_normalized)
            else request.previous_version
        )

        response = DeckResponse(
            version=next_version,
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
        self._cache_set(cache_key, response)
        return response

    async def _enforce_land_count(
        self,
        decklist: list[MagicCard],
        command_zone: list[dict[str, Any]],
        recommendations: dict[str, list[dict[str, Any]]],
        excluded_names: set[str],
        notes: list[str],
        protected: Optional[set[str]] = None,
    ) -> list[MagicCard]:
        """Keep a built deck between ``_MIN_LANDS`` and ``_MAX_LANDS`` lands (needs hydrated types).

        Too few lands: the lowest-priority spells (end of the list) become basics.
        Too many: excess lands (basics from the largest pile first) are swapped for the
        most-played EDHREC spells not already in the deck. The deck size is unchanged;
        ``_balance_basics`` then decides which colors the basics should be.
        """
        identity: set[str] = set()
        for commander in command_zone:
            identity.update(commander.get("color_identity") or [])
        lands = _land_count(decklist)

        if lands < _MIN_LANDS:
            added = _swap_spells_for_basics(
                decklist, identity, _MIN_LANDS - lands, lands, notes, protected
            )
            if added:
                await self._hydrate_cards(added)
            return decklist

        if lands > _MAX_LANDS:
            excess = lands - _MAX_LANDS
            in_deck = {card.name.lower() for card in decklist}
            in_deck.update((c.get("name") or "").lower() for c in command_zone)
            candidates = _spell_candidates(recommendations, in_deck | excluded_names)
            # Hydrate a few spares: EDHREC's mixed groups can still contain lands.
            pool = [MagicCard(name=name, count=1, category=header) for name, header in candidates[: excess * 3]]
            if pool:
                await self._hydrate_cards(pool)
            picks = [card for card in pool if card.type_line and not _is_land(card)][:excess]
            if not picks:
                notes.append(
                    f"Deck has {lands} lands (more than {_MAX_LANDS}); no replacement spells were "
                    "available, so consider cutting a few lands yourself."
                )
                return decklist
            removed = _remove_lands(decklist, len(picks))
            decklist.extend(picks)
            notes.append(
                f"Deck had {lands} lands (more than {_MAX_LANDS}); swapped {_describe(removed)} for "
                f"popular EDHREC picks: {', '.join(card.name for card in picks)}."
            )
        return decklist

    async def _balance_basics(
        self,
        decklist: list[MagicCard],
        command_zone: list[dict[str, Any]],
        notes: list[str],
    ) -> list[MagicCard]:
        """Match the basic-land split to the deck's color needs (requires hydrated costs)."""
        added = _balance_basic_lands(decklist, command_zone, notes)
        if added:
            await self._hydrate_cards(added)
        return decklist

    def _llm_failure_message(self, what: str) -> str:
        """Explain an LLM failure to the user, including the (client-safe) reason when known."""
        label = getattr(self._llm, "provider_label", None) or "The LLM"
        reason = getattr(self._llm, "last_failure_reason", None)
        detail = f": {reason}." if reason else "."
        return (
            f"{label} failed to produce a valid {what}{detail} Retry, or clear the deck concept "
            "to use a heuristic build."
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
        prefetched_cards: Optional[dict[str, dict[str, Any]]] = None,
    ) -> tuple[list[MagicCard], str, str, list[str], list[DeckSubstitution]]:
        """Turn raw LLM or heuristic output into normalized cards plus metadata."""
        notes: list[str] = []
        substitutions: list[DeckSubstitution] = []
        if llm_result and isinstance(llm_result.get("decklist"), list):
            # The LLM no longer writes oracle text (it was most of its output and slowed
            # every call); fill it from Scryfall so normalization can still spot
            # "any number of cards named" cards like Relentless Rats.
            raw_list = _with_scryfall_text(llm_result["decklist"], prefetched_cards or {})
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


def _decklist_signature(decklist: list[MagicCard]) -> list[tuple[str, int]]:
    """Create a stable deck identity using card names and counts only."""
    return sorted(
        ((card.name.strip().lower(), int(card.count or 1)) for card in decklist if card.name.strip()),
        key=lambda item: (item[0], item[1]),
    )


def _with_scryfall_text(
    raw_list: list[Any], prefetched_cards: dict[str, dict[str, Any]]
) -> list[Any]:
    """Copy oracle text, type line and card faces from prefetched Scryfall data onto raw LLM entries."""
    enriched: list[Any] = []
    for entry in raw_list:
        if isinstance(entry, dict):
            data = prefetched_cards.get(str(entry.get("name") or "").strip().lower())
            if data:
                entry = dict(entry)
                if not entry.get("oracle_text"):
                    entry["oracle_text"] = data.get("oracle_text") or ""
                if not entry.get("type_line") and data.get("type_line"):
                    entry["type_line"] = data["type_line"]
                if data.get("card_faces") and "card_faces" not in entry:
                    entry["card_faces"] = data["card_faces"]
        enriched.append(entry)
    return enriched


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

        # Check if the card allows any number in a deck (including checking card_faces layout)
        oracle_text = (entry.get("oracle_text") or "").lower()
        allows_any_number = "a deck can have any number of cards named" in oracle_text
        
        if not allows_any_number and "card_faces" in entry:
            allows_any_number = any(
                "a deck can have any number of cards named" in (face.get("oracle_text") or "").lower()
                for face in entry["card_faces"]
            )

        # Handle duplicate entries found in the raw input parsing stream
        if key in seen:
            if is_basic or allows_any_number:
                seen[key].count += count
            else:
                # Enforce singleton behavior for normal duplicate rows
                pass 
            continue  # Explicitly move to next entry

        # Enforce count = 1 for normal cards on their very first structural pass
        if not is_basic and not allows_any_number:
            count = 1
            
        type_line = entry.get("type_line") if isinstance(entry.get("type_line"), str) else None
        seen[key] = MagicCard(name=name, count=count, category=category, type_line=type_line)

    # Calculate current size state
    total = sum(card.count for card in seen.values())
    
    if total > total_cards_for_deck:
        notes.append(f"Trimmed {total - total_cards_for_deck} extra cards to reach {total_cards_for_deck}.")
        _trim_to_size(seen, total_cards_for_deck)
    elif total < total_cards_for_deck:
        notes.append(f"Padded {total_cards_for_deck - total} basic lands to reach {total_cards_for_deck}.")
        _pad_with_basics(seen, commander_card, total_cards_for_deck - total)

    return list(seen.values())


def _is_land(card: MagicCard) -> bool:
    """Best-effort land check before/after hydration (type line, then name/category).

    Only the front face counts: a "Sorcery // Land" modal card is a spell for land-count
    purposes, so it can't quietly stand in for one of the deck's 35+ real lands.
    """
    if card.name in _BASIC_NAMES:
        return True
    if card.type_line:
        return "Land" in card.type_line.split("//")[0]
    return "land" in (card.category or "").lower()


# Land-count bounds for every LLM-built or LLM-revamped deck (front-face lands only).
# 35 is the floor; the LLM is asked for 35-38.
_MIN_LANDS = 35
_MAX_LANDS = 40


def _land_count(deck: Iterable[MagicCard]) -> int:
    return sum(card.count for card in deck if _is_land(card))


def _describe(cards: list[tuple[str, int]]) -> str:
    """Render [("Forest", 2), ("Sol Ring", 1)] as '2 Forest, Sol Ring'."""
    return ", ".join(f"{count} {name}" if count > 1 else name for name, count in cards)


def _swap_spells_for_basics(
    deck: list[MagicCard],
    identity: set[str],
    needed: int,
    lands: int,
    notes: list[str],
    protected: Optional[set[str]] = None,
) -> list[MagicCard]:
    """Replace the last ``needed`` spells with basics; return any new basic entries.

    Mana rocks and dorks are only cut as a last resort: LLMs tend to list ramp right
    before the lands, and swapping Sol Ring for a Forest trades one mana source for a
    worse one.
    """
    removed: list[tuple[str, int]] = []
    keep = {name.lower() for name in (protected or set())}
    spells = [card for card in reversed(list(deck)) if not _is_land(card)]
    unprotected = [card for card in spells if card.name.lower() not in keep]
    non_ramp = [card for card in unprotected if not card.produced_mana]
    ramp = [card for card in unprotected if card.produced_mana]
    last_resort = [card for card in spells if card.name.lower() in keep]
    for card in non_ramp + ramp + last_resort:
        while card.count > 0 and len(removed) < needed:
            card.count -= 1
            removed.append((card.name, 1))
        if card.count <= 0:
            deck.remove(card)
        if len(removed) >= needed:
            break
    if not removed:
        return []

    # Any basic will do here - _balance_basic_lands assigns the colors afterwards.
    existing = next((card for card in deck if card.name in _BASIC_NAMES), None)
    added: list[MagicCard] = []
    if existing is not None:
        existing.count += len(removed)
    else:
        color = next((c for c in "WUBRG" if c in identity), None)
        name = _BASIC_BY_COLOR[color] if color else "Wastes"
        new_card = MagicCard(name=name, count=len(removed), category="Land")
        deck.append(new_card)
        added.append(new_card)
    notes.append(
        f"Deck had only {lands} lands; swapped its last {len(removed)} spell(s) "
        f"({_describe(removed)}) for basic lands to reach {lands + len(removed)}."
    )
    return added


def _remove_lands(deck: list[MagicCard], count: int) -> list[tuple[str, int]]:
    """Remove ``count`` lands: basics from the largest pile first, then nonbasics from the end."""
    removed: Counter = Counter()
    for _ in range(count):
        basics = [card for card in deck if card.name in _BASIC_NAMES and card.count > 0]
        if basics:
            pile = max(basics, key=lambda card: card.count)
            pile.count -= 1
            removed[pile.name] += 1
            if pile.count <= 0:
                deck.remove(pile)
            continue
        nonbasic = next((card for card in reversed(deck) if _is_land(card)), None)
        if nonbasic is None:
            break
        deck.remove(nonbasic)
        removed[nonbasic.name] += 1
    return list(removed.items())


def _spell_candidates(
    recommendations: dict[str, list[dict[str, Any]]], excluded: set[str]
) -> list[tuple[str, str]]:
    """Nonland EDHREC picks not in ``excluded``, most-played first, as (name, header)."""
    seen: set[str] = set()
    ranked: list[tuple[int, str, str]] = []
    for header, cards in (recommendations or {}).items():
        if "land" in header.lower():
            continue
        for card in cards:
            name = (card.get("name") or "").strip()
            key = name.lower()
            if not name or key in excluded or key in seen:
                continue
            seen.add(key)
            ranked.append((int(card.get("num_decks") or 0), name, header))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [(name, header) for _, name, header in ranked]


def _trim_to_size(deck: dict[str, MagicCard], target: int) -> None:
    """Trim an oversized deck to ``target`` while keeping its land-to-spell ratio.

    LLMs often overshoot 99 cards. Cutting only basics (the old behavior) silently
    gutted the mana base, so cuts are split between lands and spells in proportion to
    how many of each the list has. Lands lose basics from the largest pile first;
    spells and nonbasic lands are cut from the end of the list (lowest priority).
    """
    total = sum(c.count for c in deck.values())
    excess = total - target
    if excess <= 0:
        return
    land_total = sum(c.count for c in deck.values() if _is_land(c))
    land_cuts = round(excess * land_total / total) if total else 0
    spell_cuts = excess - land_cuts

    def cut_land() -> bool:
        piles = [c for c in deck.values() if c.name in _BASIC_NAMES and c.count > 1]
        if piles:
            max(piles, key=lambda c: c.count).count -= 1
            return True
        for key in reversed(list(deck)):
            if _is_land(deck[key]) and deck[key].name not in _BASIC_NAMES:
                del deck[key]
                return True
        return False

    def cut_spell() -> bool:
        for key in reversed(list(deck)):
            if not _is_land(deck[key]):
                del deck[key]
                return True
        return False

    for _ in range(land_cuts):
        if not cut_land():
            spell_cuts += 1
    for _ in range(spell_cuts):
        if not cut_spell() and not cut_land():
            break
    # Last resort (e.g. a list of nothing but single basics): drop from the end.
    while sum(c.count for c in deck.values()) > target and deck:
        last = next(reversed(list(deck)))
        if deck[last].count > 1:
            deck[last].count -= 1
        else:
            del deck[last]


_MANA_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def _color_pips(mana_cost: Optional[str]) -> Counter:
    """Count colored mana symbols in a cost; hybrid symbols split their weight.

    "{2}{W}{W}" -> W:2, "{W/U}" -> W:0.5 U:0.5, "{2/R}" and "{G/P}" -> 1 each.
    """
    pips: Counter = Counter()
    for symbol in _MANA_SYMBOL_RE.findall(mana_cost or ""):
        colors = [part for part in symbol.upper().split("/") if part in _BASIC_BY_COLOR]
        for color in colors:
            pips[color] += 1 / len(colors)
    return pips


def _deck_color_pips(
    cards: Iterable[Any], command_zone: Iterable[dict[str, Any]], identity: Iterable[str]
) -> dict[str, float]:
    """Total colored pips across nonland cards plus the commander(s), within the identity."""
    pips: Counter = Counter()
    for card in cards:
        type_line = getattr(card, "type_line", None) or ""
        if "Land" in type_line or (not type_line and getattr(card, "category", None) == "Land"):
            continue
        for color, value in _color_pips(getattr(card, "mana_cost", None)).items():
            pips[color] += value * max(1, int(getattr(card, "count", 1) or 1))
    for commander in command_zone:
        # The commander is cast repeatedly from the command zone, so its colors matter
        # more than any single card in the 99.
        for color, value in _color_pips(commander.get("mana_cost")).items():
            pips[color] += value * 2
    allowed = {c for c in identity if c in _BASIC_BY_COLOR}
    return {color: pips[color] for color in "WUBRG" if color in allowed and pips[color] > 0}


def _allocate_proportionally(weights: dict[str, float], total: int) -> dict[str, int]:
    """Split ``total`` across keys in proportion to ``weights`` (largest-remainder rounding).

    Every key with a positive weight gets at least one when ``total`` allows, so a
    lightly-used color still has a basic to fetch.
    """
    weight_sum = sum(weights.values())
    if total <= 0 or weight_sum <= 0:
        return {key: 0 for key in weights}
    exact = {key: weight / weight_sum * total for key, weight in weights.items()}
    counts = {key: int(value) for key, value in exact.items()}
    by_remainder = sorted(
        weights, key=lambda key: (exact[key] - counts[key], weights[key]), reverse=True
    )
    for key in by_remainder[: total - sum(counts.values())]:
        counts[key] += 1
    if total >= len(weights):
        for key in [k for k, v in counts.items() if v == 0]:
            donor = max(counts, key=lambda k: (counts[k], weights[k]))
            counts[donor] -= 1
            counts[key] += 1
    return counts


# Nonland mana permanents (rocks, dorks) count for less than a land: they cost a card
# and a turn, and creatures in particular are easy to lose.
_NONLAND_SOURCE_WEIGHT = 0.5
_PERMANENT_TYPES = ("Artifact", "Creature", "Enchantment", "Planeswalker", "Battle")
_FETCH_TARGET_RE = re.compile(r"search your library for (?:an?|up to \w+) (.+?) cards?\b")


def _fetchable_colors(oracle_text: Optional[str], identity: set[str]) -> set[str]:
    """Colors a fetch land can find: named basic types, or any color for 'basic land'."""
    text = (oracle_text or "").lower()
    match = _FETCH_TARGET_RE.search(text)
    if not match:
        return set()
    target = match.group(1)
    colors = {color for color, basic in _BASIC_BY_COLOR.items() if basic.lower() in target}
    if not colors and "basic land" in target:
        colors = set(identity)
    return colors & identity


def _colored_sources(deck: Iterable[MagicCard], identity: set[str]) -> dict[str, float]:
    """Colored mana sources already in the deck, excluding the colored basics.

    Lands count 1 per color they can produce (fetch lands count for each basic type
    they can find); mana rocks and dorks count ``_NONLAND_SOURCE_WEIGHT``. "Any color"
    sources such as Command Tower or Arcane Signet count for every identity color.
    One-shot spells (Dark Ritual, Cultivate) and colorless sources are ignored.
    """
    sources: Counter = Counter()
    for card in deck:
        if card.name in _BASIC_NAMES:
            continue
        type_line = card.type_line or ""
        is_land = "Land" in type_line
        if not is_land and not any(t in type_line for t in _PERMANENT_TYPES):
            continue
        colors = {c for c in (card.produced_mana or []) if c in identity}
        if not colors and is_land:
            colors = _fetchable_colors(card.oracle_text, identity)
        weight = (1.0 if is_land else _NONLAND_SOURCE_WEIGHT) * max(1, card.count or 1)
        for color in colors:
            sources[color] += weight
    return dict(sources)


def _allocate_basics(demand: dict[str, float], existing: dict[str, float], total: int) -> dict[str, int]:
    """Give basics to whichever colors are most under-supplied relative to demand.

    Each color's supply is its existing colored sources plus the basics given so far;
    every basic goes to the color with the highest demand share per source (a
    D'Hondt-style allocation that starts from the existing sources). With no other
    sources this is a plain proportional split. Every demanded color gets at least
    one basic when ``total`` allows, so fetch effects always have a target.
    """
    counts = {color: 0 for color in demand}
    demand_sum = sum(demand.values())
    if total <= 0 or demand_sum <= 0:
        return counts
    if total >= len(demand):
        counts = {color: 1 for color in demand}
    for _ in range(total - sum(counts.values())):
        neediest = max(
            demand,
            key=lambda c: (demand[c] / demand_sum / (existing.get(c, 0) + counts[c] + 1), demand[c]),
        )
        counts[neediest] += 1
    return counts


def _balance_basic_lands(
    deck: list[MagicCard],
    command_zone: list[dict[str, Any]],
    notes: list[str],
) -> list[MagicCard]:
    """Redistribute the deck's colored basics so total colored sources match color needs.

    Demand is each color's share of mana symbols; supply already includes nonbasic
    lands, mana rocks and mana creatures, so basics fill in the colors those leave
    short. The total number of basics is unchanged; only the Plains/Island/Swamp/
    Mountain/Forest split moves. Wastes and nonbasic cards are never touched. Returns
    any newly-added basic entries so the caller can hydrate them.
    """
    identity: set[str] = set()
    for commander in command_zone:
        identity.update(commander.get("color_identity") or [])
    color_by_basic = {name: color for color, name in _BASIC_BY_COLOR.items()}
    basics = {card.name: card for card in deck if card.name in color_by_basic}
    total_basics = sum(card.count for card in basics.values())
    pips = _deck_color_pips(deck, command_zone, identity)
    if total_basics == 0 or not pips:
        return []

    existing = _colored_sources(deck, identity)
    targets = {
        _BASIC_BY_COLOR[color]: n
        for color, n in _allocate_basics(pips, existing, total_basics).items()
    }
    before = {name: card.count for name, card in basics.items() if card.count}
    if before == {name: n for name, n in targets.items() if n}:
        return []

    for name, card in basics.items():
        card.count = targets.get(name, 0)
    deck[:] = [card for card in deck if not (card.name in color_by_basic and card.count <= 0)]
    added: list[MagicCard] = []
    for name, count in targets.items():
        if count > 0 and name not in basics:
            new_card = MagicCard(name=name, count=count, category="Land")
            deck.append(new_card)
            added.append(new_card)

    pip_total = sum(pips.values())
    shares = ", ".join(f"{color} {round(value / pip_total * 100)}%" for color, value in pips.items())
    split = ", ".join(f"{count} {name}" for name, count in targets.items() if count)
    totals = {
        color: existing.get(color, 0) + targets.get(_BASIC_BY_COLOR[color], 0) for color in pips
    }
    supply = ", ".join(f"{color} {_fmt_sources(value)}" for color, value in totals.items())
    other = " after counting nonbasic lands, mana rocks and dorks" if existing else ""
    notes.append(
        f"Balanced {total_basics} basic lands to the deck's color needs ({shares}){other}: "
        f"{split}. Colored sources now: {supply}."
    )
    return added


def _fmt_sources(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _pad_with_basics(deck: dict[str, MagicCard], commander_card: dict[str, Any], needed: int) -> None:
    """Pad an undersized deck with basics, weighted by mana pips when costs are known.

    During normalization cards usually don't have Scryfall data yet, so this often
    falls back to an even split; ``_balance_basic_lands`` corrects the split after
    hydration.
    """
    identity = commander_card.get("color_identity") or []
    pips = _deck_color_pips(deck.values(), [], identity)
    if not pips:
        basics = [_BASIC_BY_COLOR[c] for c in identity if c in _BASIC_BY_COLOR] or ["Wastes"]
        _distribute_round_robin(deck, basics, needed)
        return
    for color, count_to_add in _allocate_proportionally(pips, needed).items():
        if count_to_add <= 0:
            continue
        basic_name = _BASIC_BY_COLOR[color]
        key = basic_name.lower()
        if key in deck:
            deck[key].count += count_to_add
        else:
            deck[key] = MagicCard(name=basic_name, count=count_to_add, category="Land")


def _distribute_round_robin(deck, basics, needed):
    """Fallback helper for even distribution."""
    for i in range(needed):
        basic = basics[i % len(basics)]
        key = basic.lower()
        if key in deck:
            deck[key].count += 1
        else:
            deck[key] = MagicCard(name=basic, count=1, category="Land")

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


def _drop_invalid_revamp_cards(
    llm_result: dict[str, Any],
    valid_cards: dict[str, dict[str, Any]],
    invalid_labels: list[str],
    previous_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    """Remove cards that failed validation from a revamp, reverting any swap that used one.

    A swap whose added card is off-color, banned or unknown is undone as a whole: it is
    dropped from the suggestions and the card(s) it removed go back into the deck, so the
    deck isn't left a card short and the UI never offers an illegal swap.
    """
    reasons = {}
    for label in dict.fromkeys(invalid_labels):
        name, _, reason = label.rpartition(" (")
        reasons[(name or label).strip().lower()] = reason.rstrip(")") or "invalid"

    def is_valid(entry: Any) -> bool:
        return isinstance(entry, dict) and str(entry.get("name") or "").strip().lower() in valid_cards

    decklist = [entry for entry in llm_result.get("decklist") or [] if is_valid(entry)]
    removed_from_list = sorted(
        {
            str(entry.get("name"))
            for entry in llm_result.get("decklist") or []
            if isinstance(entry, dict) and not is_valid(entry)
        }
    )
    in_deck = {str(entry.get("name") or "").strip().lower() for entry in decklist}
    previous_by_name = {
        str(card.get("name") or "").strip().lower(): card for card in previous_payload
    }

    kept_subs: list[Any] = []
    reverted: list[str] = []
    for sub in llm_result.get("substitutions") or []:
        added = [a for a in (sub.get("added") or []) if isinstance(a, dict)] if isinstance(sub, dict) else []
        bad = [a for a in added if not is_valid(a)]
        if not bad:
            kept_subs.append(sub)
            continue
        for card in sub.get("removed") or []:
            key = str((card or {}).get("name") or "").strip().lower()
            if key and key not in in_deck and key in previous_by_name:
                decklist.append(dict(previous_by_name[key]))
                in_deck.add(key)
        removed_names = ", ".join(str(c.get("name")) for c in sub.get("removed") or [] if isinstance(c, dict))
        bad_names = ", ".join(
            f"{a.get('name')} ({reasons.get(str(a.get('name') or '').strip().lower(), 'invalid')})" for a in bad
        )
        reverted.append(f"{removed_names or '(nothing)'} -> {bad_names}")

    notes = list(llm_result.get("notes") or [])
    if removed_from_list:
        notes.append(f"Removed invalid card(s) from the list: {', '.join(removed_from_list)}.")
    if reverted:
        notes.append(
            "Reverted swap(s) that added cards outside the deck's color identity or "
            f"legality, keeping the original card(s): {'; '.join(reverted)}."
        )
    return {**llm_result, "decklist": decklist, "substitutions": kept_subs, "notes": notes}


def _exploration_note(pool: CardPool, decklist: list[MagicCard]) -> list[str]:
    """Summarize how many recent releases and lesser-played theme cards made the deck."""
    in_deck = {card.name.lower() for card in decklist}

    def listing(tag: str) -> list[str]:
        return [c.name for c in pool.cards if c.key in in_deck and tag in c.tags]

    def describe(names: list[str], label: str) -> str:
        shown = ", ".join(names[:6]) + ("..." if len(names) > 6 else "")
        return f"{len(names)} {label} ({shown})"

    parts = []
    if new_cards := listing("new"):
        parts.append(describe(new_cards, "recent release(s)"))
    if gems := listing("gem"):
        parts.append(describe(gems, "lesser-played theme card(s)"))
    return [f"Beyond the usual staples: {'; '.join(parts)}."] if parts else []


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
        f"basic lands matching the commander's color identity. Configure an LLM API "
        f"key (Gemini or OpenAI) to enable LLM-guided card selection and explanations."
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
