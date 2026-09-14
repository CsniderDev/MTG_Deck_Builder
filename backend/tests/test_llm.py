from app.services.llm import LLMService, _compact_decklist_for_prompt, _is_retryable_gemini_error


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


def test_compact_decklist_for_prompt_keeps_only_core_fields():
    compact = _compact_decklist_for_prompt([
        {
            "name": "Sol Ring",
            "count": 1,
            "category": "Ramp",
            "oracle_text": "Add mana.",
            "image_uris": {"normal": "https://img.test/sol-ring.jpg"},
        }
    ])
    assert compact == [{"name": "Sol Ring", "count": 1, "category": "Ramp"}]


def test_is_retryable_gemini_error_detects_unavailable_text():
    assert _is_retryable_gemini_error(RuntimeError("503 UNAVAILABLE")) is True
    assert _is_retryable_gemini_error(RuntimeError("hard failure")) is False
