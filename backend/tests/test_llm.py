from types import SimpleNamespace

import httpx
import openai
import pytest

from app.config import get_settings
from app.services.llm import LLMService, _compact_decklist_for_prompt
from app.services.llm_providers import (
    GeminiProvider,
    LLMEmptyResponseError,
    OpenAIProvider,
)

_OK_JSON = '{"decklist": [], "explanation": "ok"}'


class _FakeProvider:
    """Provider stand-in that replays a scripted list of response texts/exceptions."""

    name = "fake"
    label = "Fake"
    model = "fake-model"

    def __init__(self, responses, retryable=lambda exc: False):
        self._responses = list(responses)
        self._retryable = retryable
        self.calls = 0

    async def generate_json_text(self, prompt):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def is_retryable(self, exc):
        return self._retryable(exc)


def _service_with_env(monkeypatch, **env):
    """Build an LLMService from the given env vars (overriding anything in backend/.env)."""
    for key in ("LLM_PROVIDER", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, env.get(key, ""))
    get_settings.cache_clear()
    try:
        return LLMService()
    finally:
        get_settings.cache_clear()


def _service_with_fake(monkeypatch, responses, **overrides):
    """Build an enabled LLMService whose provider is a scripted fake."""
    svc = _service_with_env(monkeypatch, LLM_PROVIDER="gemini", GEMINI_API_KEY="test-key")
    svc._provider = _FakeProvider(responses)
    for attr, value in overrides.items():
        setattr(svc, attr, value)
    return svc


# --- JSON extraction / prompt helpers ------------------------------------------


def test_extract_json_plain():
    assert LLMService._extract_json(_OK_JSON) == {"decklist": [], "explanation": "ok"}


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


# --- Provider selection ---------------------------------------------------------


def test_service_disabled_without_key(monkeypatch):
    svc = _service_with_env(monkeypatch, LLM_PROVIDER="gemini")
    assert svc.enabled is False
    assert svc.provider_name is None


def test_gemini_selected_by_default(monkeypatch):
    svc = _service_with_env(monkeypatch, GEMINI_API_KEY="test-key")
    assert svc.enabled is True
    assert isinstance(svc._provider, GeminiProvider)
    assert svc.provider_label == "Gemini"


def test_openai_selected_when_configured(monkeypatch):
    svc = _service_with_env(monkeypatch, LLM_PROVIDER="OpenAI", OPENAI_API_KEY="sk-test")
    assert svc.enabled is True
    assert isinstance(svc._provider, OpenAIProvider)
    assert svc.provider_label == "ChatGPT"


def test_openai_without_its_key_is_disabled_even_if_gemini_key_set(monkeypatch):
    svc = _service_with_env(monkeypatch, LLM_PROVIDER="openai", GEMINI_API_KEY="test-key")
    assert svc.enabled is False


def test_unknown_provider_is_disabled(monkeypatch):
    svc = _service_with_env(monkeypatch, LLM_PROVIDER="claude", GEMINI_API_KEY="test-key")
    assert svc.enabled is False


# --- Provider adapters ----------------------------------------------------------


class _FakeOpenAICompletions:
    def __init__(self, response):
        self._response = response
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


def _openai_provider_returning(content, finish_reason="stop", refusal=None):
    provider = OpenAIProvider("sk-test", "gpt-test")
    message = SimpleNamespace(content=content, refusal=refusal)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])
    completions = _FakeOpenAICompletions(response)
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return provider, completions


@pytest.mark.asyncio
async def test_openai_provider_uses_json_mode_and_returns_text():
    provider, completions = _openai_provider_returning(_OK_JSON)
    text = await provider.generate_json_text("Respond in JSON")
    assert text == _OK_JSON
    assert completions.kwargs["model"] == "gpt-test"
    assert completions.kwargs["response_format"] == {"type": "json_object"}
    assert completions.kwargs["messages"] == [{"role": "user", "content": "Respond in JSON"}]


@pytest.mark.asyncio
async def test_openai_provider_raises_on_empty_content():
    provider, _ = _openai_provider_returning(None, finish_reason="content_filter", refusal="no")
    with pytest.raises(LLMEmptyResponseError, match="content_filter"):
        await provider.generate_json_text("Respond in JSON")


def _openai_error(cls, status, message):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return cls(message, response=response, body=None)


def test_openai_retryable_errors():
    provider = OpenAIProvider("sk-test", "gpt-test")
    assert provider.is_retryable(_openai_error(openai.RateLimitError, 429, "slow down")) is True
    assert provider.is_retryable(_openai_error(openai.InternalServerError, 500, "oops")) is True
    assert (
        provider.is_retryable(_openai_error(openai.RateLimitError, 429, "insufficient_quota"))
        is False
    )
    assert provider.is_retryable(_openai_error(openai.BadRequestError, 400, "bad")) is False


def test_gemini_retryable_errors_detect_unavailable_text():
    provider = GeminiProvider("test-key", "gemini-test")
    assert provider.is_retryable(RuntimeError("503 UNAVAILABLE")) is True
    assert provider.is_retryable(RuntimeError("hard failure")) is False


# --- Shared retry / budget / circuit breaker logic --------------------------------


@pytest.mark.asyncio
async def test_generate_json_retries_transient_errors(monkeypatch):
    monkeypatch.setattr("app.services.llm.asyncio.sleep", _no_sleep)
    svc = _service_with_fake(monkeypatch, [])
    svc._provider = _FakeProvider(
        [RuntimeError("503"), _OK_JSON], retryable=lambda exc: "503" in str(exc)
    )
    assert await svc._generate_json("prompt") == {"decklist": [], "explanation": "ok"}
    assert svc._provider.calls == 2


async def _no_sleep(_delay):
    return None


@pytest.mark.asyncio
async def test_empty_response_counts_as_failure(monkeypatch):
    svc = _service_with_fake(monkeypatch, [LLMEmptyResponseError("finish_reason=length")])
    assert await svc._generate_json("prompt") is None
    assert svc._consecutive_failures == 1


@pytest.mark.asyncio
async def test_daily_budget_blocks_calls_once_exhausted(monkeypatch):
    svc = _service_with_fake(monkeypatch, [_OK_JSON, _OK_JSON], _daily_call_limit=1)
    first = await svc._generate_json("prompt")
    second = await svc._generate_json("prompt")
    assert first == {"decklist": [], "explanation": "ok"}
    assert second is None
    assert svc._provider.calls == 1
    assert svc.calls_today == 1
    assert svc.daily_call_limit == 1


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_repeated_failures(monkeypatch):
    svc = _service_with_fake(
        monkeypatch,
        [RuntimeError("hard failure"), RuntimeError("hard failure")],
        _circuit_failure_threshold=2,
        _circuit_cooldown_seconds=60.0,
    )
    assert await svc._generate_json("prompt") is None
    assert await svc._generate_json("prompt") is None
    assert await svc._generate_json("prompt") is None
    # Only the first two calls reached the provider; the third was short-circuited
    # by the now-open circuit breaker.
    assert svc._provider.calls == 2


@pytest.mark.asyncio
async def test_circuit_breaker_resets_after_a_success(monkeypatch):
    svc = _service_with_fake(
        monkeypatch,
        [RuntimeError("hard failure"), _OK_JSON, RuntimeError("hard failure")],
        _circuit_failure_threshold=2,
        _circuit_cooldown_seconds=60.0,
    )
    await svc._generate_json("prompt")  # failure #1
    await svc._generate_json("prompt")  # success resets the streak
    result = await svc._generate_json("prompt")  # failure #1 again, still below threshold
    assert result is None
    assert svc._circuit_open() is False
