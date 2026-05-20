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
  "notes": ["<optional short bullet>", "..."],
  "substitutions": [
    {
      "removed": [{"name": "<removed card>", "count": <int>}],
      "added": [{"name": "<added card>", "count": <int>}],
      "explanation": "<why this change improves the deck>"
    }
  ]
}
Rules:
- The list must total exactly 99 cards (the commander is separate) unless there 
  is a partner commander or a background card, the non-commander list must total at most 98 cards to allow for them.
- Use the commander's color identity only. Never include cards outside it.
- Basic lands ("Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes") may
  repeat with count > 1; every other card must have count == 1 (singleton) UNLESS the card specifies otherwise, like "Hare Apparent"
  or other similar cards.
- Cover ramp, card draw, removal/interaction, threats, and an appropriate land
  count for the bracket and curve.
- Respect the bracket constraints described in the user message, including the
  game-changer limit. Treat the user's deck concept as the primary direction
  for card selection - EDHREC recommendations are a candidate pool, not a
  mandate. Substitute freely to honor the concept.
- When building from scratch, return an empty ``substitutions`` array.
- When revising an existing deck, include a ``substitutions`` entry for each
  meaningful card-for-card or package-for-package change, with concise reasons.
- Universes Beyond cards and cards with alternate printed names are valid if they 
  are Commander-legal and resolvable via Scryfall. Prefer official Scryfall names in
  the final output when possible. Please verify that all commander legal cards are considered
  especially from the newer sets and universes beyond - treat them as real options if they fit the concept and constraints.
- For categories, do not over complicate. If there are certain standout synergies 
  or themes, label them with short tags like "ramp", "interaction", "card draw", 
  "win condition", "Board Wipe", "Theme Support" (but replace the word theme with the actual theme of the deck),
  etc. Otherwise, just use "Utility" for everything that doesn't fit into a small number of obvious categories.
""".strip()


def _gamechanger_block(
    limit: Optional[int], gamechangers: Optional[Iterable[str]]
) -> str:
    """Render a prompt block describing the bracket's game-changer constraint."""
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
        """Initialize the Gemini client when an API key is configured."""
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
        """Report whether Gemini-backed deck generation is currently available."""
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
        banned_list: Optional[Iterable[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """Ask Gemini to build a new commander deck from a commander and concept prompt."""
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
            banned_list,
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
        banned_list: Optional[Iterable[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """Ask Gemini to revise an existing decklist and explain each substitution."""
        if not self._enabled or self._client is None:
            return None
        gc_block = _gamechanger_block(gamechanger_limit, gamechangers)


        prompt = (
            f"You are revising an existing Magic: The Gathering Commander deck.\n"
            f"Commander: {commander.get('name')} "
            f"(color identity: {''.join(commander.get('color_identity') or []) or 'C'}).\n"
            f"Bracket {bracket} - {bracket_description}\n"
            f"Original user concept: {user_prompt or '(none)'}\n"
            f"User change request: {change_request}\n\n"
            f"Here is a current list of banned cards. DO NOT include any of these cards at all:\n{json.dumps(list(banned_list or []))}\n\n"
            f"{gc_block}"
            f"Current decklist (99 cards, JSON):\n{json.dumps(previous_decklist)}\n\n"
            f"Produce a revised 99-card decklist applying the change request while "
            f"keeping the deck coherent and legal. For every meaningful swap, include "
            f"a substitution entry listing the removed card(s), the added card(s), and "
            f"a concise explanation of why the substitution helps. Treat Universes Beyond "
            f"cards and alternate printed or reskinned names as valid whenever Scryfall "
            f"resolves them to Commander-legal cards. Prefer official Scryfall card names "
            f"in the final JSON when possible.\n\n"
            f"{_DECK_SCHEMA_INSTRUCTIONS}"
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
        banned_list: Optional[Iterable[str]] = None,
    ) -> str:
        """Construct the build-from-scratch prompt sent to Gemini."""
        ci = "".join(commander.get("color_identity") or []) or "C"
        rec_lines: list[str] = []
        for header, cards in recommendations.items():
            names = ", ".join(c["name"] for c in cards[:40] if c.get("name"))
            if names:
                rec_lines.append(f"- {header}: {names}")
        
        recs_block = "\n".join(rec_lines) if rec_lines else "(no EDHREC data available)"
        concept = user_prompt or "(none provided - use the commander's strengths)"
        gc_block = _gamechanger_block(gamechanger_limit, gamechangers)

        budget_block = ""
        budgetConstraint = r"/(?:budget|max|limit|under|cost|price)\s*[\$]?\s*(\d+(?:\.\d{2})?)|[\$](\d+(?:\.\d{2})?)\s*(?:dollars|bucks|usd)?/i;"
        budgetMatch = re.search(budgetConstraint, user_prompt, re.IGNORECASE) if user_prompt else None
        dollar_pattern = r"[\$](\d+(?:\.\d{2})?)"

        if budgetMatch:
            budget_block = f"The budget for the deck must be at or under: ${budgetMatch.group(1) or budgetMatch.group(2)}\n\n"
        
        budgetMatch = re.search(dollar_pattern, user_prompt, re.IGNORECASE) if user_prompt else None
        if budgetMatch and not budget_block:
            budget_block = f"The user mentioned a budget of ${budgetMatch.group(1)}, but it was not clear if this was a constraint. If it is a constraint, the deck must be at or under this budget.\n\n"
        return (
            f"You are an expert Magic: The Gathering Commander deck builder.\n\n"
            f"Commander: {commander.get('name')}\n"
            f"Type line: {commander.get('type_line')}\n"
            f"Color identity: {ci}\n"
            f"Oracle text: {commander.get('oracle_text') or '(none)'}\n\n"
            f"Bracket {bracket}: {bracket_description}\n\n"
            f"{gc_block}"
            f"{budget_block}"
            f"User's deck concept / prompt (PRIMARY DIRECTION - build around this):\n"
            f"{concept}\n\n"
            f"Here is a current list of banned cards. DO NOT include any of these cards at all:\n{json.dumps(list(banned_list or []))}\n\n"
            f"Remember the commander's color identity is {ci}. Only include cards that share colors with the commander. "
            f"Universes Beyond cards and cards with alternate printed names are still valid if they are Commander-legal and Scryfall-resolvable. "
            f"Prefer official Scryfall card names in the final JSON when possible.\n\n"
            f"EDHREC recommendation pool (reference candidates only - substitute "
            f"whenever the user's concept or bracket constraints demand it):\n"
            f"{recs_block}\n\n{_DECK_SCHEMA_INSTRUCTIONS}"
        )

    async def _generate_json(self, prompt: str) -> Optional[dict[str, Any]]:
        """Send a prompt to Gemini and parse the JSON-mode response."""
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
        """Extract the first JSON object from raw model output, tolerating fences."""
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
