"""Client for the public EDHREC JSON endpoints.

EDHREC exposes the same data its website consumes at
``https://json.edhrec.com/pages/commanders/<slug>.json``. The slug is the
commander name lower-cased with spaces replaced by hyphens and apostrophes
stripped. The payload contains ``container.json_dict.cardlists``: a list of
themed groups (Top Cards, High Synergy, Lands, Creatures, Ramp, etc.), each
with ``cardviews`` containing card metadata.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from ..config import get_settings


class EDHRecError(RuntimeError):
    pass


def commander_to_slug(name: str) -> str:
    slug = name.lower().split(" //")[0]
    slug = re.sub(r"[\u2019']", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


class EDHRecClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        settings = get_settings()
        self._base = settings.edhrec_base_url
        self._client = client or httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": "MTG-Deck-Builder/0.1", "Accept": "application/json"},
        )
        self._own_client = client is None
        self._page_cache: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def commander_page(self, commander_name: str) -> dict[str, Any]:
        slug = commander_to_slug(commander_name)
        if slug in self._page_cache:
            return self._page_cache[slug]
        url = f"{self._base}/pages/commanders/{slug}.json"
        response = await self._client.get(url)
        if response.status_code == 404:
            raise EDHRecError(f"EDHREC has no page for commander slug {slug!r}.")
        if response.status_code >= 400:
            raise EDHRecError(f"EDHREC {response.status_code} for {slug!r}")
        page = response.json()
        self._page_cache[slug] = page
        return page

    @staticmethod
    def extract_recommendations(page: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """Return ``{category_header: [card_dict, ...]}`` from an EDHREC page."""
        container = page.get("container") or {}
        json_dict = container.get("json_dict") or {}
        cardlists = json_dict.get("cardlists") or []
        result: dict[str, list[dict[str, Any]]] = {}
        for group in cardlists:
            header = group.get("header") or group.get("tag") or "Other"
            cards: list[dict[str, Any]] = []
            for view in group.get("cardviews", []) or []:
                cards.append(
                    {
                        "name": view.get("name"),
                        "sanitized": view.get("sanitized"),
                        "num_decks": view.get("num_decks"),
                        "potential_decks": view.get("potential_decks"),
                        "synergy": view.get("synergy"),
                        "salt": view.get("salt"),
                        "label": view.get("label"),
                    }
                )
            if cards:
                result[header] = cards
        return result
