from app.services.llm import LLMService


def test_extract_json_plain():
    text = '{"decklist": [], "explanation": "ok"}'
    assert LLMService._extract_json(text) == {"decklist": [], "explanation": "ok"}


def test_extract_json_with_fenced_block():
    text = """Sure! Here you go:
```json
{"decklist": [{"name": "Sol Ring", "count": 1}], "explanation": "ramp"}
```
Thanks."""
    parsed = LLMService._extract_json(text)
    assert parsed["decklist"][0]["name"] == "Sol Ring"


def test_extract_json_with_surrounding_prose():
    text = 'Output: {"explanation": "test", "decklist": []} done.'
    parsed = LLMService._extract_json(text)
    assert parsed == {"explanation": "test", "decklist": []}


def test_extract_json_returns_none_on_garbage():
    assert LLMService._extract_json("definitely not json") is None
    assert LLMService._extract_json("") is None
    assert LLMService._extract_json("{ not: valid }") is None


def test_service_disabled_without_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from app.config import get_settings
    get_settings.cache_clear()
    svc = LLMService()
    assert svc.enabled is False
