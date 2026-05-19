"""Thin async client over the public Scryfall REST API.

Scryfall is free and unauthenticated; we keep a polite per-request delay and a
shared httpx.AsyncClient. See https://scryfall.com/docs/api.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from ..config import get_settings


_REQUEST_DELAY = 0.075  # Scryfall asks for 50-100ms between calls.


class ScryfallError(RuntimeError):
    pass


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

    async def aclose(self) -> None:
        """Close the shared HTTP client when this instance owns it."""
        if self._own_client:
            await self._client.aclose()

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Perform a rate-limited GET request against the Scryfall API."""
        async with self._lock:
            await asyncio.sleep(_REQUEST_DELAY)
            response = await self._client.get(f"{self._base}{path}", params=params)
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
        legal = (card.get("legalities") or {}).get("commander") == "legal"
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
        """Fetch a single card by exact name first, then fuzzy fallback if needed."""
        try:
            return await self.named(name, fuzzy=False)
        except ScryfallError:
            try:
                return await self.named(name, fuzzy=True)
            except ScryfallError:
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
        try:
            data = await self._get(
                "/cards/autocomplete", {"q": query, "include_extras": "false"}
            )
        except ScryfallError:
            return []
        return list((data.get("data") or [])[:limit])


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
        unique_names = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
        if not unique_names:
            return []

        missing_names = [name for name in unique_names if name.lower() not in self._collection_cache]
        chunk_size = 75

        for i in range(0, len(missing_names), chunk_size):
            chunk = missing_names[i:i + chunk_size]
            payload = {"identifiers": [{"name": n} for n in chunk]}
            response_data = await self._post("/cards/collection", payload)
            for card in response_data.get("data", []):
                card_name = (card.get("name") or "").strip().lower()
                if card_name:
                    self._collection_cache[card_name] = card

        return [
            self._collection_cache[name.lower()]
            for name in unique_names
            if name.lower() in self._collection_cache
        ]

    async def _post(self, path: str, json_data: dict) -> dict:
        """Perform a rate-limited POST request against the Scryfall API."""
        try:
            async with self._lock:
                await asyncio.sleep(_REQUEST_DELAY)
                response = await self._client.post(
                    f"{self._base}{path}",
                    json=json_data,
                    timeout=httpx.Timeout(60.0, connect=10.0),
                )
        except httpx.ReadTimeout as exc:
            raise ScryfallError("Scryfall timed out while processing the card collection.") from exc

        if response.status_code == 404:
            raise ScryfallError(f"Not found: {path}")
        if response.status_code >= 400:
            raise ScryfallError(f"Scryfall {response.status_code}: {response.text[:200]}")
        return response.json()