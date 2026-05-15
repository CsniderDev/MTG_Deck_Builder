"""Thin async client over the public Scryfall REST API.

Scryfall is free and unauthenticated; we keep a polite per-request delay and a
shared httpx.AsyncClient. See https://scryfall.com/docs/api.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional
from venv import logger

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


    async def getBannedList(
        self
    ) -> list[str]:
        try:
            data = await self._get(
                "/cards/search",
                {
                    "q": f"banned:commander",
                    "order": "edhrec",
                    "unique": "cards",
                },
            )
            logger.info("Fetched banned list: %d cards", len(data.get("data") or []))
        except ScryfallError:
            return []
        seen: list[str] = []
        for card in data.get("data") or []:
            name = card.get("name")
            if name and name not in seen:
                seen.append(name)
        logger.info("Fetched banned list: %d cards", len(seen))
        return seen



    async def getGamechangers(
            self
    ) -> list[str]:
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

        logger.info("Fetched gamechangers: %d cards", len(seen)) 
        return seen



    async def get_collection(self, names: list[str]) -> list[dict]:
        # Scryfall limit is 75, but 50 is safer and faster for large payloads
        unique_names = list(set(n for n in names if n))
    
        chunk_size = 50
        all_data = []

        for i in range(0, len(unique_names), chunk_size):
            chunk = unique_names[i:i + chunk_size]
            payload = {"identifiers": [{"name": n} for n in chunk]}

            # This will now use the increased timeout from your _post method
            response_data = await self._post("/cards/collection", payload)
            all_data.extend(response_data.get("data", []))

            # Small sleep to be a good citizen and avoid 429 Rate Limits
            await asyncio.sleep(0.1) 

        return all_data

    async def _post(self, path: str, json_data: dict) -> dict:
        """
        Helper for POST requests with increased timeout to prevent ReadTimeout errors.
        """
        # Define a generous timeout: 10s to connect, 60s to read the data
        timeout = httpx.Timeout(60.0, connect=10.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            url = f"https://api.scryfall.com{path}"
            headers = {
                "User-Agent": "MTG-Deck-Builder-App/1.0", 
                "Accept": "application/json"
            }
            
            try:
                response = await client.post(url, json=json_data, headers=headers)
                
                if response.status_code != 200:
                    raise ScryfallError(f"Scryfall API error: {response.status_code} {response.text}")
                    
                return response.json()
            except httpx.ReadTimeout:
                # Re-raising as your custom error or logging it specifically
                raise ScryfallError("Scryfall timed out while processing the card collection.")