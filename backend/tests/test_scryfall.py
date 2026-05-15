import httpx
import pytest
import respx

from app.services.scryfall import ScryfallClient, ScryfallError


def _atraxa():
    return {
        "name": "Atraxa, Praetors' Voice",
        "type_line": "Legendary Creature - Phyrexian Angel Horror",
        "oracle_text": "Flying, vigilance, deathtouch, lifelink",
        "legalities": {"commander": "legal"},
        "color_identity": ["W", "U", "B", "G"],
    }


@pytest.mark.asyncio
async def test_resolve_commander_accepts_legendary_creature():
    with respx.mock(base_url="https://api.scryfall.com") as router:
        router.get("/cards/named").mock(return_value=httpx.Response(200, json=_atraxa()))
        client = ScryfallClient()
        try:
            card = await client.resolve_commander("atraxa")
        finally:
            await client.aclose()
    assert card["name"] == "Atraxa, Praetors' Voice"


@pytest.mark.asyncio
async def test_resolve_commander_accepts_planeswalker_with_clause():
    card_payload = {
        "name": "Freyalise, Llanowar's Fury",
        "type_line": "Legendary Planeswalker - Freyalise",
        "oracle_text": "Freyalise, Llanowar's Fury can be your commander.",
        "legalities": {"commander": "legal"},
        "color_identity": ["G"],
    }
    with respx.mock(base_url="https://api.scryfall.com") as router:
        router.get("/cards/named").mock(return_value=httpx.Response(200, json=card_payload))
        client = ScryfallClient()
        try:
            card = await client.resolve_commander("freyalise")
        finally:
            await client.aclose()
    assert card["name"].startswith("Freyalise")


@pytest.mark.asyncio
async def test_resolve_commander_rejects_non_legendary():
    payload = {
        "name": "Llanowar Elves",
        "type_line": "Creature - Elf Druid",
        "oracle_text": "{T}: Add {G}.",
        "legalities": {"commander": "legal"},
        "color_identity": ["G"],
    }
    with respx.mock(base_url="https://api.scryfall.com") as router:
        router.get("/cards/named").mock(return_value=httpx.Response(200, json=payload))
        client = ScryfallClient()
        try:
            with pytest.raises(ScryfallError):
                await client.resolve_commander("llanowar elves")
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_resolve_commander_rejects_banned_commander():
    payload = {
        "name": "Golos, Tireless Pilgrim",
        "type_line": "Legendary Artifact Creature - Scout",
        "oracle_text": "...",
        "legalities": {"commander": "banned"},
        "color_identity": ["W", "U", "B", "R", "G"],
    }
    with respx.mock(base_url="https://api.scryfall.com") as router:
        router.get("/cards/named").mock(return_value=httpx.Response(200, json=payload))
        client = ScryfallClient()
        try:
            with pytest.raises(ScryfallError):
                await client.resolve_commander("golos")
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_named_404_raises():
    with respx.mock(base_url="https://api.scryfall.com") as router:
        router.get("/cards/named").mock(return_value=httpx.Response(404))
        client = ScryfallClient()
        try:
            with pytest.raises(ScryfallError):
                await client.named("doesnotexist")
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_autocomplete_commanders_returns_unique_names_in_order():
    payload = {
        "data": [
            {"name": "Atraxa, Praetors' Voice"},
            {"name": "Atraxa, Grand Unifier"},
            {"name": "Atraxa, Praetors' Voice"},  # duplicate, should be dropped
            {"name": "Atraxa, Praetors' Voice"},
        ]
    }
    with respx.mock(base_url="https://api.scryfall.com") as router:
        route = router.get("/cards/search").mock(return_value=httpx.Response(200, json=payload))
        client = ScryfallClient()
        try:
            names = await client.autocomplete_commanders("atra")
        finally:
            await client.aclose()
    assert names == ["Atraxa, Praetors' Voice", "Atraxa, Grand Unifier"]
    call = route.calls.last.request
    assert "is%3Acommander" in str(call.url) or "is:commander" in str(call.url)


@pytest.mark.asyncio
async def test_autocomplete_commanders_returns_empty_on_404():
    with respx.mock(base_url="https://api.scryfall.com") as router:
        router.get("/cards/search").mock(return_value=httpx.Response(404))
        client = ScryfallClient()
        try:
            names = await client.autocomplete_commanders("zzzzz")
        finally:
            await client.aclose()
    assert names == []


@pytest.mark.asyncio
async def test_autocomplete_commanders_empty_query_skips_request():
    with respx.mock(base_url="https://api.scryfall.com", assert_all_called=False) as router:
        route = router.get("/cards/search").mock(return_value=httpx.Response(200, json={"data": []}))
        client = ScryfallClient()
        try:
            assert await client.autocomplete_commanders("") == []
            assert await client.autocomplete_commanders("   ") == []
        finally:
            await client.aclose()
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_autocomplete_commanders_broad_mode_uses_autocomplete_endpoint():
    payload = {"data": ["Lightning Bolt", "Lightning Strike", "Lightning Helix"]}
    with respx.mock(base_url="https://api.scryfall.com") as router:
        route = router.get("/cards/autocomplete").mock(return_value=httpx.Response(200, json=payload))
        client = ScryfallClient()
        try:
            names = await client.autocomplete_commanders("light", commander_only=False, limit=2)
        finally:
            await client.aclose()
    assert names == ["Lightning Bolt", "Lightning Strike"]
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_get_card_falls_back_to_fuzzy():
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(404)
        return httpx.Response(200, json=_atraxa())

    with respx.mock(base_url="https://api.scryfall.com") as router:
        router.get("/cards/named").mock(side_effect=handler)
        client = ScryfallClient()
        try:
            card = await client.get_card("atraxa")
        finally:
            await client.aclose()
    assert card is not None
    assert card["name"] == "Atraxa, Praetors' Voice"
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_get_collection_caches_cards_across_calls():
    payload = {
        "data": [
            {"name": "Sol Ring", "legalities": {"commander": "legal"}},
            {"name": "Arcane Signet", "legalities": {"commander": "legal"}},
        ]
    }
    with respx.mock(base_url="https://api.scryfall.com") as router:
        route = router.post("/cards/collection").mock(return_value=httpx.Response(200, json=payload))
        client = ScryfallClient()
        try:
            cards = await client.get_collection(["Sol Ring", "Arcane Signet"])
            again = await client.get_collection(["Sol Ring"])
        finally:
            await client.aclose()
    assert [card["name"] for card in cards] == ["Sol Ring", "Arcane Signet"]
    assert [card["name"] for card in again] == ["Sol Ring"]
    assert route.call_count == 1
