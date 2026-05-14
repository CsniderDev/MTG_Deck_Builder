"""Gemini wrapper that returns structured deck JSON.

Uses the modern ``google-genai`` SDK (``from google import genai``). If no API
key is configured we simply return ``None`` from the public methods so the
deck builder can fall back to a heuristic. This keeps the app usable without
any LLM credentials.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional

from google import genai
from google.genai import types as genai_types

from ..config import get_settings

logger = logging.getLogger(__name__)


_DECK_SCHEMA_INSTRUCTIONS = """
Respond ONLY with a single JSON object, no prose, no markdown fences. Schema:
{
  "decklist": [{"name": "<exact card name>", "count": <int>, "category": "<short label>"}],
  "explanation": "<2-4 paragraph plain-text explanation of the deck plan>",
  "notes": ["<optional short bullet>", "..."]
}
Rules:
- The list must total exactly 99 cards (the commander is separate).
- Use the commander's color identity only. Never include cards outside it.
- Basic lands ("Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes") may
  repeat with count > 1; every other card must have count == 1 (singleton).
- Cover ramp, card draw, removal/interaction, threats, and an appropriate land
  count for the bracket and curve.
- Respect the bracket constraints described in the user message, including the
  game-changer limit. Treat the user's deck concept as the primary direction
  for card selection - EDHREC recommendations are a candidate pool, not a
  mandate. Substitute freely to honor the concept.
""".strip()


def _gamechanger_block(
    limit: Optional[int], gamechangers: Optional[Iterable[str]]
) -> str:
    if gamechangers is None:
        return ""
    names = sorted(set(gamechangers))
    if limit is None:
        rule = (
            "Game-changers are unrestricted at this bracket; include them as the "
            "concept and curve dictate."
        )
    elif limit == 0:
        rule = (
            "ZERO game-changers are allowed at this bracket. Do NOT include any "
            "card from the list below."
        )
    else:
        rule = (
            f"AT MOST {limit} game-changer(s) are allowed at this bracket. "
            f"Include no more than {limit} card(s) from the list below."
        )
    return (
        "Game-changer constraint:\n"
        f"{rule}\n"
        f"Official game-changer list: {', '.join(names)}\n\n"
    )


class LLMService:
    def __init__(self) -> None:
        settings = get_settings()
        self._enabled = bool(settings.gemini_api_key)
        self._model_name = settings.gemini_model
        if self._enabled:
            self._client: Optional[genai.Client] = genai.Client(api_key=settings.gemini_api_key)
            logger.info("LLMService initialized with model=%s", self._model_name)
        else:
            self._client = None
            logger.info("LLMService disabled: GEMINI_API_KEY is not set")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def build_deck(
        self,
        commander: dict[str, Any],
        bracket: int,
        bracket_description: str,
        user_prompt: str,
        recommendations: dict[str, list[dict[str, Any]]],
        gamechanger_limit: Optional[int] = None,
        gamechangers: Optional[Iterable[str]] = None,
        invalid_card_names: Optional[Iterable[str]] = None,
    ) -> Optional[dict[str, Any]]:
        if not self._enabled or self._client is None:
            return None
        prompt = self._build_prompt(
            commander,
            bracket,
            bracket_description,
            user_prompt,
            recommendations,
            gamechanger_limit,
            gamechangers,
            invalid_card_names,
        )
        return await self._generate_json(prompt)

    async def revamp_deck(
        self,
        commander: dict[str, Any],
        bracket: int,
        bracket_description: str,
        user_prompt: str,
        previous_decklist: list[dict[str, Any]],
        change_request: str,
        gamechanger_limit: Optional[int] = None,
        gamechangers: Optional[Iterable[str]] = None,
        invalid_card_names: Optional[Iterable[str]] = None,
    ) -> Optional[dict[str, Any]]:
        if not self._enabled or self._client is None:
            return None
        gc_block = _gamechanger_block(gamechanger_limit, gamechangers)

        invalid_cards = ''
        if invalid_card_names:
            invalid_block = "\n".join(f"- {name}" for name in invalid_card_names)
            invalid_cards += f"\nThe following cards are invalid and need to be replaced:\n{invalid_block}\n\n"        

        prompt = (
            f"You are revising an existing Magic: The Gathering Commander deck.\n"
            f"Commander: {commander.get('name')} "
            f"(color identity: {''.join(commander.get('color_identity') or []) or 'C'}).\n"
            f"Bracket {bracket} - {bracket_description}\n"
            f"Original user concept: {user_prompt or '(none)'}\n"
            f"User change request: {change_request}\n\n"
            f"{invalid_cards}"
            f"{gc_block}"
            f"Current decklist (99 cards, JSON):\n{json.dumps(previous_decklist)}\n\n"
            f"Produce a revised 99-card decklist applying the change request while "
            f"keeping the deck coherent and legal.\n\n{_DECK_SCHEMA_INSTRUCTIONS}"
        )
        return await self._generate_json(prompt)

    def _build_prompt(
        self,
        commander: dict[str, Any],
        bracket: int,
        bracket_description: str,
        user_prompt: str,
        recommendations: dict[str, list[dict[str, Any]]],
        gamechanger_limit: Optional[int] = None,
        gamechangers: Optional[Iterable[str]] = None,
        invalid_card_names: Optional[Iterable[str]] = None,
    ) -> str:
        ci = "".join(commander.get("color_identity") or []) or "C"
        rec_lines: list[str] = []
        for header, cards in recommendations.items():
            names = ", ".join(c["name"] for c in cards[:40] if c.get("name"))
            if names:
                rec_lines.append(f"- {header}: {names}")
        recs_block = "\n".join(rec_lines) if rec_lines else "(no EDHREC data available)"
        concept = user_prompt or "(none provided - use the commander's strengths)"
        gc_block = _gamechanger_block(gamechanger_limit, gamechangers)
        invalid_cards = ''
        if invalid_card_names:
            invalid_block = "\n".join(f"- {name}" for name in invalid_card_names)
            invalid_cards += f"\nThe following cards are invalid and need to be replaced:\n{invalid_block}\n\n"
        return (
            f"You are an expert Magic: The Gathering Commander deck builder.\n\n"
            f"Commander: {commander.get('name')}\n"
            f"Type line: {commander.get('type_line')}\n"
            f"Color identity: {ci}\n"
            f"Oracle text: {commander.get('oracle_text') or '(none)'}\n\n"
            f"Bracket {bracket}: {bracket_description}\n\n"
            f"{gc_block}"
            f"User's deck concept / prompt (PRIMARY DIRECTION - build around this):\n"
            f"{concept}\n\n"
            f"{invalid_cards}"
            f"EDHREC recommendation pool (reference candidates only - substitute "
            f"whenever the user's concept or bracket constraints demand it):\n"
            f"{recs_block}\n\n{_DECK_SCHEMA_INSTRUCTIONS}"
        )

    async def _generate_json(self, prompt: str) -> Optional[dict[str, Any]]:
        if self._client is None:
            return None
        logger.info(
            "Gemini request: model=%s prompt_chars=%d", self._model_name, len(prompt)
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - propagate as None, log details
            logger.exception("Gemini request raised: %s", exc)
            return None
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            feedback = getattr(response, "prompt_feedback", None)
            candidates = getattr(response, "candidates", None) or []
            finish = getattr(candidates[0], "finish_reason", None) if candidates else None
            logger.error(
                "Gemini returned empty text. finish_reason=%s prompt_feedback=%s",
                finish,
                feedback,
            )
            return None
        logger.info("Gemini response: %d chars", len(text))
        parsed = self._extract_json(text)
        if parsed is None:
            logger.error(
                "Gemini output was non-empty but JSON parsing failed. First 500 chars: %s",
                text[:500],
            )
        return parsed

    @staticmethod
    def _extract_json(text: str) -> Optional[dict[str, Any]]:
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fence.group(1) if fence else text
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from LLM output: %s", text[:300])
            return None
