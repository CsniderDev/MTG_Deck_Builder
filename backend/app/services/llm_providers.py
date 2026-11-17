"""Provider adapters behind ``LLMService``.

Each adapter knows how to send one prompt to its vendor in JSON mode and return
the raw text, and which of its vendor's exceptions are worth retrying. Prompting,
JSON parsing, retries, the daily call budget and the circuit breaker all live in
``LLMService`` and are shared by every provider.

Select a provider with ``LLM_PROVIDER`` (``gemini`` or ``openai``) and give it the
matching API key (``GEMINI_API_KEY`` / ``OPENAI_API_KEY``).
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol

from ..config import Settings

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("gemini", "openai")


class LLMEmptyResponseError(Exception):
    """Raised when a provider answers without any text; the message carries diagnostics."""


class LLMProvider(Protocol):
    name: str
    label: str
    model: str

    async def generate_json_text(self, prompt: str) -> str:
        """Send ``prompt`` in JSON mode and return the response text (non-empty)."""
        ...

    def is_retryable(self, exc: Exception) -> bool:
        """Return True for transient failures worth retrying."""
        ...


def _message_looks_transient(exc: Exception) -> bool:
    message = str(exc).upper()
    return any(token in message for token in ("503", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "429"))


class GeminiProvider:
    name = "gemini"
    label = "Gemini"

    def __init__(self, api_key: str, model: str, thinking_budget: int = -1) -> None:
        from google import genai
        from google.genai import types as genai_types

        self.model = model
        self._thinking_budget = thinking_budget
        # Pin the API version: google-genai 3.x defaults to "v1", which rejects JSON mode
        # ("JSON mode is not enabled for api version v1"). v1beta works on 2.x and 3.x.
        self._client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(api_version="v1beta"),
        )

    async def generate_json_text(self, prompt: str) -> str:
        from google.genai import types as genai_types

        response = await self._client.aio.models.generate_content(
            model=self.model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=(
                    genai_types.ThinkingConfig(thinking_budget=self._thinking_budget)
                    if self._thinking_budget >= 0
                    else None
                ),
            ),
        )
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            candidates = getattr(response, "candidates", None) or []
            finish = getattr(candidates[0], "finish_reason", None) if candidates else None
            feedback = getattr(response, "prompt_feedback", None)
            raise LLMEmptyResponseError(f"finish_reason={finish} prompt_feedback={feedback}")
        return text

    def is_retryable(self, exc: Exception) -> bool:
        from google.genai import errors as genai_errors

        if isinstance(exc, genai_errors.ServerError) and getattr(exc, "status_code", None) in {
            429,
            500,
            503,
            504,
        }:
            return True
        return _message_looks_transient(exc)


class OpenAIProvider:
    name = "openai"
    label = "ChatGPT"

    def __init__(self, api_key: str, model: str) -> None:
        from openai import AsyncOpenAI

        self.model = model
        self._client = AsyncOpenAI(api_key=api_key)

    async def generate_json_text(self, prompt: str) -> str:
        # JSON mode requires the word "JSON" somewhere in the messages; every prompt
        # LLMService sends ends with the JSON schema instructions, so that holds.
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        choice = response.choices[0] if response.choices else None
        message = getattr(choice, "message", None)
        text = (getattr(message, "content", "") or "").strip()
        if not text:
            raise LLMEmptyResponseError(
                f"finish_reason={getattr(choice, 'finish_reason', None)} "
                f"refusal={getattr(message, 'refusal', None)}"
            )
        return text

    def is_retryable(self, exc: Exception) -> bool:
        import openai

        if isinstance(exc, openai.RateLimitError):
            # Out-of-credit errors arrive as 429s too, but retrying them is pointless.
            return "insufficient_quota" not in str(exc)
        if isinstance(
            exc, (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)
        ):
            return True
        return _message_looks_transient(exc)


def create_provider(settings: Settings) -> Optional[LLMProvider]:
    """Build the configured provider, or return None when it has no API key."""
    provider = (settings.llm_provider or "gemini").strip().lower()
    if provider == "gemini":
        if not settings.gemini_api_key:
            logger.info("LLM disabled: LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
            return None
        return GeminiProvider(
            settings.gemini_api_key, settings.gemini_model, settings.gemini_thinking_budget
        )
    if provider == "openai":
        if not settings.openai_api_key:
            logger.info("LLM disabled: LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
            return None
        return OpenAIProvider(settings.openai_api_key, settings.openai_model)
    logger.error(
        "LLM disabled: unknown LLM_PROVIDER=%r (expected one of %s)",
        settings.llm_provider,
        ", ".join(SUPPORTED_PROVIDERS),
    )
    return None
