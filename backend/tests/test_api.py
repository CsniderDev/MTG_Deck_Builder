"""End-to-end API tests using FastAPI's TestClient with stubbed services."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.services.deck_builder import DeckBuilder


ATRAXA = {
    "id": "abc-123",
    "name": "Atraxa, Praetors' Voice",
    "type_line": "Legendary Creature - Phyrexian Angel Horror",
    "oracle_text": "Flying, vigilance, deathtouch, lifelink",
    "legalities": {"commander": "legal"},
    "color_identity": ["W", "U", "B", "G"],
    "colors": ["W", "U", "B", "G"],
    "mana_cost": "{G}{W}{U}{B}",
    "image_uris": {"normal": "https://img.test/atraxa-normal.jpg"},
}


class _StubScryfall:
    async def resolve_commander(self, name: str) -> dict[str, Any]:
        if "atraxa" not in name.lower():
            from app.services.scryfall import ScryfallError
            raise ScryfallError("not legal")
        return ATRAXA

    async def autocomplete_commanders(
        self, query: str, commander_only: bool = True, limit: int = 15
    ) -> list[str]:
        if not (query or "").strip():
            return []
        if "atra" in query.lower():
            return ["Atraxa, Praetors' Voice", "Atraxa, Grand Unifier"]
        return []

    async def get_collection(self, names: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "id": f"id-{name.lower().replace(' ', '-')}",
                "name": name,
                "type_line": "Artifact",
                "oracle_text": f"Rules text for {name}",
                "mana_cost": "{1}",
                "colors": [],
                "color_identity": [],
                "legalities": {"commander": "legal"},
                "image_uris": {"normal": f"https://img.test/{name.lower().replace(' ', '-')}.jpg"},
            }
            for name in names
        ]

    async def getBannedList(self) -> list[str]:
        return ["Black Lotus"]

    async def getGamechangers(self) -> list[str]:
        return ["Rhystic Study", "Smothering Tithe"]

    async def aclose(self):
        pass


class _StubEDHRec:
    async def commander_page(self, name: str) -> dict[str, Any]:
        return {}

    @staticmethod
    def extract_recommendations(page):
        return {"Top Cards": [{"name": "Sol Ring"}, {"name": "Arcane Signet"}]}

    async def aclose(self):
        pass


class _StubLLM:
    def __init__(self, payload=None, enabled=False):
        self._payload = payload
        self.enabled = enabled

    async def build_deck(self, **_):
        return self._payload

    async def revamp_deck(self, **_):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    # Build the app with stubbed services, bypassing the real lifespan.
    app = main_module.app
    scryfall = _StubScryfall()
    builder = DeckBuilder(scryfall, _StubEDHRec(), _StubLLM())
    app.state.builder = builder
    app.state.scryfall = scryfall
    app.state.llm = builder._llm  # type: ignore[attr-defined]
    return TestClient(app)


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["llm_enabled"] is False


def test_build_endpoint_success(client):
    response = client.post(
        "/api/decks/build",
        json={"commander": "Atraxa", "bracket": 3, "prompt": ""},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["bracket"] == 3
    assert body["commander"]["name"] == "Atraxa, Praetors' Voice"
    assert body["commander"]["image_uris"]["normal"] == "https://img.test/atraxa-normal.jpg"
    assert body["source"] == "heuristic"
    total = sum(c["count"] for c in body["decklist"])
    assert total == 99
    sol_ring = next(card for card in body["decklist"] if card["name"] == "Sol Ring")
    assert sol_ring["image_uris"]["normal"] == "https://img.test/sol-ring.jpg"


def test_banned_list_endpoint_returns_named_payload(client):
    response = client.get("/api/banned")
    assert response.status_code == 200
    assert response.json() == {"banned_list": ["Black Lotus"]}


def test_gamechangers_endpoint_returns_named_payload(client):
    response = client.get("/api/gamechangers")
    assert response.status_code == 200
    assert response.json() == {"gamechangers": ["Rhystic Study", "Smothering Tithe"]}


def test_build_endpoint_rejects_prompt_without_llm(client):
    response = client.post(
        "/api/decks/build",
        json={"commander": "Atraxa", "bracket": 3, "prompt": "go infinite with combos"},
    )
    assert response.status_code == 400
    assert "gemini" in response.json()["detail"].lower()


def test_build_endpoint_rejects_unknown_commander(client):
    response = client.post(
        "/api/decks/build",
        json={"commander": "Not A Commander", "bracket": 2, "prompt": ""},
    )
    assert response.status_code == 400
    assert "not legal" in response.json()["detail"].lower()


def test_build_endpoint_validates_bracket(client):
    response = client.post(
        "/api/decks/build",
        json={"commander": "Atraxa", "bracket": 7, "prompt": ""},
    )
    assert response.status_code == 422


def test_build_endpoint_requires_commander(client):
    response = client.post(
        "/api/decks/build",
        json={"commander": "", "bracket": 2, "prompt": ""},
    )
    assert response.status_code == 422


def test_autocomplete_endpoint_returns_suggestions(client):
    response = client.get("/api/cards/autocomplete", params={"q": "atra"})
    assert response.status_code == 200
    body = response.json()
    assert body["suggestions"] == [
        "Atraxa, Praetors' Voice",
        "Atraxa, Grand Unifier",
    ]


def test_autocomplete_endpoint_returns_empty_when_no_matches(client):
    response = client.get("/api/cards/autocomplete", params={"q": "zzzzzz"})
    assert response.status_code == 200
    assert response.json() == {"suggestions": []}


def test_autocomplete_endpoint_requires_query(client):
    response = client.get("/api/cards/autocomplete", params={"q": ""})
    assert response.status_code == 422


def test_revamp_endpoint_success(client):
    response = client.post(
        "/api/decks/revamp",
        json={
            "commander": "Atraxa",
            "bracket": 3,
            "prompt": "",
            "previous_version": 1,
            "previous_decklist": [{"name": "Sol Ring", "count": 1}],
            "change_request": "swap some cards",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2
    assert sum(c["count"] for c in body["decklist"]) == 99
