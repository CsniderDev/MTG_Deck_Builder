"""Thin async client over the public Scryfall REST API.

Scryfall is free and unauthenticated; we keep a polite per-request delay and a
shared httpx.AsyncClient. See https://scryfall.com/docs/api.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

import httpx

from ..config import get_settings


_REQUEST_DELAY = 0.1  # Scryfall asks for 50-100ms between calls (~10/s max).
# Card search/lookup endpoints get a stricter spacing: bursts of ~13/s to these returned
# 429s with 16-60s Retry-After penalties, which cost far more than spacing them out.
_HEAVY_ENDPOINT_DELAY = 0.5
_HEAVY_ENDPOINTS = ("/cards/search", "/cards/named", "/cards/collection", "/cards/random")
_MAX_429_RETRIES = 3
_PARTNER_WITH_RE = re.compile(r"partner with\s+([^\n(]+)", re.IGNORECASE)
_PARTNER_VARIANT_RE = re.compile(r"partner[—-]\s*([^\n(]+)", re.IGNORECASE)
_TRAILING_SET_CODE_RE = re.compile(
    r"\s*(?:\([A-Za-z0-9]{2,6}\)|\[[A-Za-z0-9]{2,6}\])(?=\s*[0-9]+[A-Za-z]?(?:\s*[*★☆])*\s*$)\s*"
)
_TRAILING_COLLECTOR_NUMBER_RE = re.compile(r"\s+[0-9]+[A-Za-z]?(?:\s*[*★☆])*\s*$")


class ScryfallError(RuntimeError):
    pass


def _normalized_text(text: str) -> str:
    return " ".join(text.replace("—", "-").lower().split())


def _scrub_card_lookup_name(name: str) -> str:
    """Strip trailing set-code / collector-number suffixes from pasted decklist names."""
    scrubbed = _TRAILING_SET_CODE_RE.sub(" ", (name or "")).strip()
    scrubbed = _TRAILING_COLLECTOR_NUMBER_RE.sub("", scrubbed).strip()
    return scrubbed


def _is_background_card(card: dict[str, Any]) -> bool:
    return "background" in (card.get("type_line") or "").lower()


def _commander_pairing_mode(card: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    oracle_text = card.get("oracle_text") or ""
    lower_text = oracle_text.lower()
    if partner_with := _PARTNER_WITH_RE.search(oracle_text):
        return "partner_with", partner_with.group(1).strip()
    if "choose a background" in lower_text:
        return "background", None
    if "doctor's companion" in lower_text:
        return "doctor", None
    if variant_match := _PARTNER_VARIANT_RE.search(oracle_text):
        return "variant", _normalized_text(variant_match.group(1))
    if re.search(r"\bpartner\b", lower_text):
        return "partner", None
    return None, None


class ScryfallClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        """Initialize the Scryfall client plus lightweight caches for repeat lookups."""
        settings = get_settings()
        self._base = settings.scryfall_base_url
        self._client = client or httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": "MTG-Deck-Builder/0.1", "Accept": "application/json"},
        )
        self._own_client = client is None
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__)
        self._named_cache: dict[tuple[str, bool], dict[str, Any]] = {}
        self._collection_cache: dict[str, dict[str, Any]] = {}
        self._banned_list_cache: Optional[list[str]] = None
        self._gamechangers_cache: Optional[list[str]] = None
        self._search_cache: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self._last_request_at: dict[str, float] = {}

    async def aclose(self) -> None:
        """Close the shared HTTP client when this instance owns it."""
        if self._own_client:
            await self._client.aclose()

    async def _throttle(self, path: str = "") -> None:
        """Space request *starts*: 500ms apart for card search/lookup endpoints, 100ms otherwise.

        Only the spacing is serialized; responses can overlap. (Holding the lock for the
        whole request made slow searches - some take several seconds - run one by one.)
        """
        heavy = path.startswith(_HEAVY_ENDPOINTS)
        bucket, delay = ("heavy", _HEAVY_ENDPOINT_DELAY) if heavy else ("light", _REQUEST_DELAY)
        async with self._lock:
            now = time.monotonic()
            wait = max(
                self._last_request_at.get(bucket, 0.0) + delay,
                self._last_request_at.get("any", 0.0) + _REQUEST_DELAY,
            ) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at[bucket] = self._last_request_at["any"] = time.monotonic()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send a throttled request, backing off and retrying on HTTP 429."""
        for attempt in range(_MAX_429_RETRIES + 1):
            await self._throttle(path)
            response = await self._client.request(method, f"{self._base}{path}", **kwargs)
            if response.status_code != 429 or attempt == _MAX_429_RETRIES:
                return response
            try:
                wait = float(response.headers.get("retry-after", ""))
            except ValueError:
                wait = 1.0 * (attempt + 1)
            self._logger.warning("Scryfall rate limit hit on %s; retrying in %.1fs", path, wait)
            async with self._lock:  # pause everyone, not just this request
                await asyncio.sleep(wait)
        return response

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Perform a rate-limited GET request against the Scryfall API."""
        response = await self._send("GET", path, params=params)
        if response.status_code == 404:
            raise ScryfallError(f"Not found: {path} {params}")
        if response.status_code >= 400:
            raise ScryfallError(f"Scryfall {response.status_code}: {response.text[:200]}")
        return response.json()

    async def named(self, name: str, fuzzy: bool = True) -> dict[str, Any]:
        """Look up a card by name, optionally using Scryfall's fuzzy matching."""
        cache_key = (name.strip().lower(), fuzzy)
        if cache_key in self._named_cache:
            return self._named_cache[cache_key]
        key = "fuzzy" if fuzzy else "exact"
        card = await self._get("/cards/named", {key: name})
        self._named_cache[cache_key] = card
        return card

    async def resolve_commander(self, name: str) -> dict[str, Any]:
        """Resolve a name to a Commander-legal commander card object."""
        card = await self.named(name, fuzzy=True)
        type_line = (card.get("type_line") or "").lower()
        oracle = (card.get("oracle_text") or "").lower()
        legal = True  # (card.get("legalities") or {}).get("commander") == "legal"
        is_legendary_creature = "legendary" in type_line and "creature" in type_line
        is_planeswalker_commander = (
            "planeswalker" in type_line and "can be your commander" in oracle
        )
        is_background_partner_pair = False  # single-card resolution only for now
        if not legal or not (is_legendary_creature or is_planeswalker_commander or is_background_partner_pair):
            raise ScryfallError(
                f"{card.get('name', name)!r} is not a valid Commander-legal commander."
            )
        return card
    
    

    async def get_card(self, name: str) -> Optional[dict[str, Any]]:
        """Fetch a single card by exact name first, then fuzzy fallback, then search as last resort."""
        name = _scrub_card_lookup_name(name)
        if not name:
            return None
        try:
            return await self.named(name, fuzzy=False)
        except ScryfallError:
            pass

        try:
            return await self.named(name, fuzzy=True)
        except ScryfallError:
            pass
            
        # Universes Beyond / Flavor Name Fallback
        # Scryfall's named endpoint often fails on flavor names, but the search endpoint catches them.
        try:
            data = await self._get(
                "/cards/search",
                {
                    "q": f'"{name}"', 
                    "include_extras": "true"
                }
            )
            cards = data.get("data") or []
            if cards:
                return cards[0]
        except ScryfallError:
            pass

        return None

    async def autocomplete_commanders(
        self, query: str, commander_only: bool = True, limit: int = 15
    ) -> list[str]:
        """Return commander suggestions for a query using Scryfall search endpoints."""
        query = (query or "").strip()
        if not query:
            return []
        if commander_only:
            try:
                data = await self._get(
                    "/cards/search",
                    {
                        "q": f"is:commander name:{query}",
                        "order": "edhrec",
                        "unique": "cards",
                    },
                )
            except ScryfallError:
                return []
            seen: list[str] = []
            for card in data.get("data") or []:
                name = card.get("name")
                if name and name not in seen:
                    seen.append(name)
                    if len(seen) >= limit:
                        break
            return seen

    async def resolve_commander(self, name: str) -> dict[str, Any]:
        """Resolve a name to a Commander-legal commander card object."""
        # Use our robust get_card instead of the strict named() method
        card = await self.get_card(name)
        if not card:
            raise ScryfallError(
                f"Could not find card matching {name!r}."
            )

        type_line = (card.get("type_line") or "").lower()
        oracle = (card.get("oracle_text") or "").lower()
        legal = True#(card.get("legalities") or {}).get("commander") == "legal"
        is_legendary_creature = "legendary" in type_line and "creature" in type_line
        is_planeswalker_commander = (
            "planeswalker" in type_line and "can be your commander" in oracle
        )
        is_background_partner_pair = False  # single-card resolution only for now
        
        if not legal or not (is_legendary_creature or is_planeswalker_commander or is_background_partner_pair):
            raise ScryfallError(
                f"{card.get('name', name)!r} is not a valid Commander-legal commander."
            )
        return card

    async def get_commander_companion_options(self, name: str) -> dict[str, Any]:
        """Return compatible secondary commander/background choices for a commander."""
        card = await self.named(name, fuzzy=True)
        mode, detail = _commander_pairing_mode(card)
        card_name = card.get("name") or name
        if mode == "partner_with" and detail:
            return {
                "primary_name": card_name,
                "relationship": "Partner with",
                "secondary_kind": "commander",
                "options": [detail],
            }
        if mode == "background":
            options = await self._search_card_names("t:background legal:commander", exclude_name=card_name)
            return {
                "primary_name": card_name,
                "relationship": "Background",
                "secondary_kind": "background",
                "options": options,
            }
        if mode == "doctor":
            options = await self._search_card_names("t:doctor is:commander legal:commander", exclude_name=card_name)
            return {
                "primary_name": card_name,
                "relationship": "Doctor's companion",
                "secondary_kind": "commander",
                "options": options,
            }
        if mode in {"partner", "variant"}:
            partners = await self._search_cards("is:partner legal:commander")
            options: list[str] = []
            for candidate in partners:
                candidate_name = candidate.get("name")
                if not candidate_name or candidate_name == card_name:
                    continue
                candidate_mode, candidate_detail = _commander_pairing_mode(candidate)
                if mode == "partner" and candidate_mode == "partner":
                    options.append(candidate_name)
                elif mode == "variant" and candidate_mode == "variant" and candidate_detail == detail:
                    options.append(candidate_name)
            relationship = "Partner" if mode == "partner" else f"Partner — {detail.title() if detail else 'Variant'}"
            return {
                "primary_name": card_name,
                "relationship": relationship,
                "secondary_kind": "commander",
                "options": sorted(options),
            }
        return {
            "primary_name": card_name,
            "relationship": None,
            "secondary_kind": None,
            "options": [],
        }


    async def getBannedList(
        self
    ) -> list[str]:
        """Fetch and cache the Commander banned list from Scryfall search."""
        if self._banned_list_cache is not None:
            return list(self._banned_list_cache)
        try:
            data = await self._get(
                "/cards/search",
                {
                    "q": f"banned:commander",
                    "order": "edhrec",
                    "unique": "cards",
                },
            )
            self._logger.info("Fetched banned list: %d cards", len(data.get("data") or []))
        except ScryfallError:
            return []
        seen: list[str] = []
        for card in data.get("data") or []:
            name = card.get("name")
            if name and name not in seen:
                seen.append(name)
        self._logger.info("Fetched banned list: %d cards", len(seen))
        self._banned_list_cache = list(seen)
        return seen



    async def getGamechangers(
            self
    ) -> list[str]:
        """Fetch and cache the Commander game-changer list from Scryfall search."""
        if self._gamechangers_cache is not None:
            return list(self._gamechangers_cache)
        try:
            data = await self._get(
                "/cards/search",
                {
                    "q": f"is:gamechanger",
                    "order": "edhrec",
                    "unique": "cards",
                },
            )
        except ScryfallError:
            return []
        seen: list[str] = []
        for card in data.get("data") or []:
            name = card.get("name")
            if name and name not in seen:
                seen.append(name)       

        self._logger.info("Fetched gamechangers: %d cards", len(seen)) 
        self._gamechangers_cache = list(seen)
        return seen



    async def get_collection(self, names: list[str]) -> list[dict]:
        """Batch-fetch cards by name through ``/cards/collection`` with caching."""
        unique_names = list(
            dict.fromkeys(
                scrubbed
                for scrubbed in (_scrub_card_lookup_name(n) for n in names)
                if scrubbed
            )
        )
        if not unique_names:
            return []

        missing_names = [name for name in unique_names if name.lower() not in self._collection_cache]
        chunk_size = 75

        async def fetch_chunk(chunk: list[str]) -> dict[str, Any]:
            payload = {"identifiers": [{"name": n} for n in chunk]}
            try:
                return await self._post("/cards/collection", payload)
            except ScryfallError:
                # If the batch completely fails, queue them all up for individual recovery
                return {"data": [], "not_found": [{"name": n} for n in chunk]}

        # Batches run concurrently (request starts are still spaced by _throttle).
        chunks = [missing_names[i:i + chunk_size] for i in range(0, len(missing_names), chunk_size)]
        responses = await asyncio.gather(*(fetch_chunk(chunk) for chunk in chunks))

        for response_data in responses:

            # 1. Cache the successfully found cards
            for card in response_data.get("data", []):
                card_name = (card.get("name") or "").strip().lower()
                flavor_name = (card.get("flavor_name") or "").strip().lower()
                
                # Cache by Oracle name
                if card_name:
                    self._collection_cache[card_name] = card
                # Cache by Flavor Name (e.g. "The Cloudsea Djinn")
                if flavor_name:
                    self._collection_cache[flavor_name] = card

            # 2. Recover cards the bulk endpoint rejected (Flavor Names, misspellings)
            not_found = response_data.get("not_found", [])
            for nf in not_found:
                nf_name = nf.get("name")
                if not nf_name:
                    continue
                
                # Fetch individually using our fallback logic
                card = await self.get_card(nf_name)
                if card:
                    self._collection_cache[nf_name.lower()] = card
                    
                    primary_name = (card.get("name") or "").strip().lower()
                    if primary_name:
                        self._collection_cache[primary_name] = card

        # 3. Return results matching the original requested list
        results = []
        for name in unique_names:
            lower_name = name.lower()
            if lower_name in self._collection_cache:
                results.append(self._collection_cache[lower_name])
            else:
                # Absolute last resort if it somehow slipped through
                card = await self.get_card(name)
                if card:
                    self._collection_cache[lower_name] = card
                    results.append(card)

        return results
    

    async def search(
        self, query: str, order: str = "edhrec", direction: str = "auto", limit: int = 175
    ) -> list[dict[str, Any]]:
        """Run a Scryfall search (first page, unique cards), cached per query.

        Returns [] when nothing matches (Scryfall answers 404) and raises ScryfallError
        for other failures such as a malformed query.
        """
        key = (query, order, direction)
        if key not in self._search_cache:
            try:
                data = await self._get(
                    "/cards/search",
                    {"q": query, "order": order, "dir": direction, "unique": "cards"},
                )
                cards = list(data.get("data") or [])
            except ScryfallError as exc:
                if str(exc).startswith("Not found"):
                    cards = []
                else:
                    raise
            if len(self._search_cache) > 500:
                self._search_cache.clear()
            self._search_cache[key] = cards
        return self._search_cache[key][:limit]

    async def _search_cards(self, query: str) -> list[dict[str, Any]]:
        """Run a Scryfall search query and return the first page of unique cards."""
        data = await self._get(
            "/cards/search",
            {
                "q": query,
                "order": "name",
                "unique": "cards",
                "include_extras": "true",
            },
        )
        return list(data.get("data") or [])

    async def _search_card_names(self, query: str, exclude_name: Optional[str] = None) -> list[str]:
        """Run a Scryfall search query and return de-duplicated card names."""
        try:
            cards = await self._search_cards(query)
        except ScryfallError:
            return []
        excluded = (exclude_name or "").strip().lower()
        seen: list[str] = []
        for card in cards:
            card_name = card.get("name")
            if not card_name:
                continue
            if card_name.lower() == excluded:
                continue
            if card_name not in seen:
                seen.append(card_name)
        return seen

    async def _post(self, path: str, json_data: dict) -> dict:
        """Perform a rate-limited POST request against the Scryfall API."""
        try:
            response = await self._send(
                "POST", path, json=json_data, timeout=httpx.Timeout(60.0, connect=10.0)
            )
        except httpx.ReadTimeout as exc:
            raise ScryfallError("Scryfall timed out while processing the card collection.") from exc

        if response.status_code == 404:
            raise ScryfallError(f"Not found: {path}")
        if response.status_code >= 400:
            raise ScryfallError(f"Scryfall {response.status_code}: {response.text[:200]}")
        return response.json()