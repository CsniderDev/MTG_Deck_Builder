"""Thin async client over the public Scryfall REST API.

Scryfall is free and unauthenticated; we keep a polite per-request delay and a
shared httpx.AsyncClient. See https://scryfall.com/docs/api.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from ..config import get_settings


_REQUEST_DELAY = 0.075  # Scryfall asks for 50-100ms between calls.


class ScryfallError(RuntimeError):
    pass


class ScryfallClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        settings = get_settings()
        self._base = settings.scryfall_base_url
        self._client = client or httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": "MTG-Deck-Builder/0.1", "Accept": "application/json"},
        )
        self._own_client = client is None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        async with self._lock:
            await asyncio.sleep(_REQUEST_DELAY)
            response = await self._client.get(f"{self._base}{path}", params=params)
        if response.status_code == 404:
            raise ScryfallError(f"Not found: {path} {params}")
        if response.status_code >= 400:
            raise ScryfallError(f"Scryfall {response.status_code}: {response.text[:200]}")
        return response.json()

    async def named(self, name: str, fuzzy: bool = True) -> dict[str, Any]:
        key = "fuzzy" if fuzzy else "exact"
        return await self._get("/cards/named", {key: name})

    async def resolve_commander(self, name: str) -> dict[str, Any]:
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
