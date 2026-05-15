import httpx
import pytest
import respx

from app.services.edhrec import EDHRecClient, EDHRecError, commander_to_slug


def test_commander_to_slug_basic():
    assert commander_to_slug("Atraxa, Praetors' Voice") == "atraxa-praetors-voice"


def test_commander_to_slug_apostrophes_and_punctuation():
    assert commander_to_slug("Kaalia of the Vast") == "kaalia-of-the-vast"
    assert commander_to_slug("Krenko, Mob Boss") == "krenko-mob-boss"
    assert commander_to_slug("Saskia the Unyielding") == "saskia-the-unyielding"


def test_commander_to_slug_handles_modal_dfc_split():
    # We only slug the front face for split/MDFC names.
    assert commander_to_slug("Esika, God of the Tree // The Prismatic Bridge") == "esika-god-of-the-tree"


@pytest.mark.asyncio
async def test_commander_page_success():
    sample = {
        "container": {
            "json_dict": {
                "cardlists": [
                    {
                        "header": "Top Cards",
                        "cardviews": [
                            {"name": "Sol Ring", "num_decks": 1000, "synergy": 0.1},
                            {"name": "Arcane Signet", "num_decks": 900},
                        ],
                    },
                    {
                        "header": "Lands",
                        "cardviews": [{"name": "Command Tower"}],
                    },
                ]
            }
        }
    }
    with respx.mock(base_url="https://json.edhrec.com") as router:
        router.get("/pages/commanders/atraxa-praetors-voice.json").mock(
            return_value=httpx.Response(200, json=sample)
        )
        client = EDHRecClient()
        try:
            page = await client.commander_page("Atraxa, Praetors' Voice")
        finally:
            await client.aclose()
    recs = EDHRecClient.extract_recommendations(page)
    assert list(recs.keys()) == ["Top Cards", "Lands"]
    assert [c["name"] for c in recs["Top Cards"]] == ["Sol Ring", "Arcane Signet"]


@pytest.mark.asyncio
async def test_commander_page_not_found_raises():
    with respx.mock(base_url="https://json.edhrec.com") as router:
        router.get("/pages/commanders/nobody.json").mock(return_value=httpx.Response(404))
        client = EDHRecClient()
        try:
            with pytest.raises(EDHRecError):
                await client.commander_page("Nobody")
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_commander_page_uses_cache_for_repeat_requests():
    sample = {"container": {"json_dict": {"cardlists": []}}}
    with respx.mock(base_url="https://json.edhrec.com") as router:
        route = router.get("/pages/commanders/atraxa-praetors-voice.json").mock(
            return_value=httpx.Response(200, json=sample)
        )
        client = EDHRecClient()
        try:
            first = await client.commander_page("Atraxa, Praetors' Voice")
            second = await client.commander_page("Atraxa, Praetors' Voice")
        finally:
            await client.aclose()
    assert first == second == sample
    assert route.call_count == 1


def test_extract_recommendations_skips_empty_groups():
    page = {"container": {"json_dict": {"cardlists": [{"header": "Empty", "cardviews": []}]}}}
    assert EDHRecClient.extract_recommendations(page) == {}
