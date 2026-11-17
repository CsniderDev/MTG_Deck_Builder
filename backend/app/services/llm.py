"""LLM wrapper that returns structured deck JSON.

The actual vendor call (Gemini or OpenAI/ChatGPT, chosen by ``LLM_PROVIDER``) lives
in ``llm_providers``; this module owns the prompts, retries, JSON parsing, the
daily call budget and the circuit breaker. If no API key is configured we simply
return ``None`` from the public methods so the deck builder can fall back to a
heuristic. This keeps the app usable without any LLM credentials.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

import httpx
from ..config import get_settings
from .llm_providers import LLMEmptyResponseError, LLMProvider, create_provider

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
- consider the theme of the commander and the user's concept as the primary direction for card selection. 
  Use the EDHREC recommendations as a reference pool, but substitute freely to honor the concept and constraints.
- Basic lands ("Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes") may
  repeat with count > 1; every other card must have count == 1 (singleton) UNLESS the card specifies otherwise, like "Hare Apparent"
  or other similar cards.
- When building out the mana base, ensure that the mana production supports the mana cost. If mana costs
  more heavily feature one color, prioritize that color in the mana base, but don't neglect the others. 
  Include appropriate mana-fixing and colorless sources if needed.
- Cover ramp, card draw, removal/interaction, threats, and an appropriate land
  count for the bracket and curve.
- Respect the bracket constraints described in the user message, including the
  game-changer limit. Treat the user's deck concept as the primary direction
  for card selection - EDHREC recommendations are a candidate pool, not a
  mandate. Substitute freely to honor the concept.
- When building from scratch, return an empty ``substitutions`` array.
- When revising an existing deck, include a ``substitutions`` entry for each
  meaningful card-for-card or package-for-package change, with concise reasons.
- Understand that if the user has a card already in their deck, then that card
  is a valid card and 
-  If there is card data provided in the attached JSON payload, it is verified, real, and exists on Scryfall. 
- Do NOT flag any card as a "hallucination," "placeholder," or "invalid card" based on your internal knowledge base. This application handles Universes Beyond (UB), flavor-name variants, and brand-new card sets that may be past your training data cutoff.
- If a card is present in the provided list, treat it as an absolutely legal, valid Magic: The Gathering card for the purposes of this deck evaluation. Trust the provided data blindly.
- Universes Beyond cards and cards with alternate printed names are valid if they 
  are Commander-legal and resolvable via Scryfall. Prefer official Scryfall names in
  the final output when possible. Please verify that all commander legal cards are considered
  especially from the newer sets and universes beyond - treat them as real options if they fit the concept and constraints.
- For categories, do not over complicate. If there are certain standout synergies 
  or themes, label them with short tags like "ramp", "interaction", "card draw", 
  "win condition", "Board Wipe", "Theme Support" (but replace the word theme with the actual theme of the deck),
  etc. Otherwise, just use "Utility" for everything that doesn't fit into a small number of obvious categories.
- You must actively consider the brand-new sets and Universes Beyond card options provided in the [New Card Pool] context below. 
- Evaluate these cards strictly by their mechanics, type lines, and oracle text provided in the payload. Do not ignore them just because they are unfamiliar to your historical training data.
- Integrate these new tools seamlessly into the deck strategy if they synergize with the Commander's theme.
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


def _compact_decklist_for_prompt(previous_decklist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce a hydrated decklist to the minimal fields the LLM needs for revisions."""
    compact_cards: list[dict[str, Any]] = []
    for card in previous_decklist:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "").strip()
        if not name:
            continue
        compact_card: dict[str, Any] = {
            "name": name,
            "count": int(card.get("count") or 1),
        }
        category = card.get("category")
        if category:
            compact_card["category"] = str(category)
        compact_cards.append(compact_card)
    return compact_cards


def _classify_llm_error(exc: Exception) -> str:
    """Map a provider exception to a short, client-safe reason (never raw upstream text)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    message = f"{type(exc).__name__} {exc}".upper()
    if status == 429 or "429" in message or "RESOURCE_EXHAUSTED" in message or "QUOTA" in message:
        return "rate limited or out of quota at the provider - wait a minute and retry"
    if status in (500, 502, 503, 504) or "UNAVAILABLE" in message or "OVERLOADED" in message:
        return "the model is overloaded or temporarily unavailable - retry shortly"
    if status in (401, 403) or "API_KEY" in message or "API KEY" in message or "PERMISSION" in message:
        return "the API key was rejected - check the key configured on the server"
    if status == 404 or "NOT_FOUND" in message:
        return "the configured model was not found - check the model setting on the server"
    if "TIMEOUT" in message or "TIMED OUT" in message:
        return "the request to the model timed out"
    if isinstance(status, int) and 400 <= status < 500:
        # Status enum (e.g. INVALID_ARGUMENT) is a fixed vocabulary, safe to show.
        enum = getattr(exc, "status", None)
        suffix = f" {enum}" if isinstance(enum, str) and enum.isupper() else ""
        return f"the provider rejected the request ({status}{suffix}) - see server logs"
    return f"unexpected error calling the model ({type(exc).__name__})"


class LLMService:
    def __init__(self) -> None:
        """Initialize the configured LLM provider when its API key is set."""
        settings = get_settings()
        self._provider: Optional[LLMProvider] = create_provider(settings)
        if self._provider is not None:
            logger.info(
                "LLMService initialized with provider=%s model=%s",
                self._provider.name,
                self._provider.model,
            )

        # --- Daily call budget: a hard ceiling on real LLM calls per UTC day. ---
        self._daily_call_limit = settings.llm_daily_call_limit
        self._call_count = 0
        self._call_count_date = self._today()
        self._alert_webhook_url = settings.alert_webhook_url
        self._alerted_thresholds: set[int] = set()

        # --- Circuit breaker: stop hammering the LLM after repeated failures. ---
        self._circuit_failure_threshold = settings.llm_circuit_failure_threshold
        self._circuit_cooldown_seconds = settings.llm_circuit_cooldown_seconds
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        # Short, client-safe description of why the most recent call returned None.
        self.last_failure_reason: Optional[str] = None

    @property
    def enabled(self) -> bool:
        """Report whether LLM-backed deck generation is currently available."""
        return self._provider is not None

    @property
    def provider_name(self) -> Optional[str]:
        """Report the active provider id ("gemini"/"openai"), or None when disabled."""
        return self._provider.name if self._provider else None

    @property
    def provider_label(self) -> Optional[str]:
        """Report a human-friendly provider name ("Gemini"/"ChatGPT"), or None when disabled."""
        return self._provider.label if self._provider else None

    @property
    def calls_today(self) -> int:
        """Report how many real LLM calls have been made so far in the current UTC day."""
        if self._today() != self._call_count_date:
            return 0
        return self._call_count

    @property
    def daily_call_limit(self) -> int:
        """Report the configured daily LLM call ceiling."""
        return self._daily_call_limit

    @staticmethod
    def _today() -> date:
        return datetime.now(timezone.utc).date()

    def _check_and_increment_budget(self) -> bool:
        """Consume one unit of the daily LLM call budget; return False once exhausted."""
        today = self._today()
        if today != self._call_count_date:
            self._call_count_date = today
            self._call_count = 0
            self._alerted_thresholds.clear()
        if self._call_count >= self._daily_call_limit:
            return False
        self._call_count += 1
        self._maybe_alert_budget_threshold()
        return True

    def _maybe_alert_budget_threshold(self) -> None:
        """Fire an optional webhook the first time usage crosses 80% and 100% of budget."""
        ratio = self._call_count / self._daily_call_limit if self._daily_call_limit else 0
        for threshold in (100, 80):
            if ratio * 100 >= threshold and threshold not in self._alerted_thresholds:
                self._alerted_thresholds.add(threshold)
                logger.warning(
                    "Daily LLM call budget at %d%% (%d/%d calls today).",
                    threshold,
                    self._call_count,
                    self._daily_call_limit,
                )
                self._send_alert_webhook(
                    f"Daily LLM call budget at {threshold}% "
                    f"({self._call_count}/{self._daily_call_limit} calls today)."
                )
                break

    def _send_alert_webhook(self, message: str) -> None:
        """Best-effort, non-blocking notification to an optional Slack-style webhook."""
        if not self._alert_webhook_url:
            return
        try:
            asyncio.get_running_loop().create_task(self._post_alert_webhook(message))
        except RuntimeError:
            logger.debug("No running event loop; skipping alert webhook.")

    async def _post_alert_webhook(self, message: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(self._alert_webhook_url, json={"text": message})
        except Exception as exc:  # noqa: BLE001 - alerting must never break the request path
            logger.warning("Failed to deliver alert webhook: %s", exc)

    def _circuit_open(self) -> bool:
        """Report whether the circuit breaker is currently open (skipping LLM calls)."""
        return time.monotonic() < self._circuit_open_until

    def _record_llm_failure(self) -> None:
        """Track a failed LLM call and open the circuit breaker after repeated failures."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._circuit_failure_threshold:
            self._circuit_open_until = time.monotonic() + self._circuit_cooldown_seconds
            logger.error(
                "LLM calls failed %d times in a row; opening circuit breaker for %.0fs.",
                self._consecutive_failures,
                self._circuit_cooldown_seconds,
            )

    def _record_llm_success(self) -> None:
        """Reset the circuit breaker's failure count after a successful LLM call."""
        self._consecutive_failures = 0

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
        """Ask the LLM to build a new commander deck from a commander and concept prompt."""
        if self._provider is None:
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
        """Ask the LLM to revise an existing decklist and explain each substitution."""
        if self._provider is None:
            return None
        gc_block = _gamechanger_block(gamechanger_limit, gamechangers)
        compact_previous_decklist = _compact_decklist_for_prompt(previous_decklist)


        prompt = (
            f"You are revising an existing Magic: The Gathering Commander deck.\n"
            f"Commander: {commander.get('name')} "
            f"(color identity: {''.join(commander.get('color_identity') or []) or 'C'}).\n"
            f"Bracket {bracket} - {bracket_description}\n"
            f"Original user concept: {user_prompt or '(none)'}\n"
            f"User change request: {change_request}\n\n"
            f"Here is a current list of banned cards. DO NOT include any of these cards at all:\n{json.dumps(list(banned_list or []))}\n\n"
            f"{gc_block}"
            f"Current decklist (99 cards, compact JSON):\n{json.dumps(compact_previous_decklist)}\n\n"
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
        """Construct the build-from-scratch prompt sent to the LLM."""
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
        """Send a prompt to the configured provider and parse the JSON-mode response."""
        provider = self._provider
        self.last_failure_reason = None
        if provider is None:
            self.last_failure_reason = "no LLM is configured"
            return None
        if self._circuit_open():
            remaining = max(0, int(self._circuit_open_until - time.monotonic()))
            self.last_failure_reason = (
                f"paused after {self._circuit_failure_threshold} failed calls in a row - "
                f"retry in about {remaining}s"
            )
            logger.warning(
                "%s circuit breaker is open after repeated failures; skipping request "
                "and falling back to the heuristic path.",
                provider.label,
            )
            return None
        if not self._check_and_increment_budget():
            self.last_failure_reason = (
                f"the daily limit of {self._daily_call_limit} LLM calls has been reached"
            )
            logger.warning(
                "Daily LLM call budget (%d) exhausted; skipping request and falling back "
                "to the heuristic path.",
                self._daily_call_limit,
            )
            return None
        logger.info(
            "%s request: model=%s prompt_chars=%d", provider.label, provider.model, len(prompt)
        )
        text: Optional[str] = None
        # Overload/rate-limit errors from free tiers usually clear within a few seconds.
        retry_delays = [0.0, 2.0, 5.0]
        for attempt, delay in enumerate(retry_delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                text = await provider.generate_json_text(prompt)
                break
            except LLMEmptyResponseError as exc:
                logger.error("%s returned empty text. %s", provider.label, exc)
                self.last_failure_reason = "the model returned an empty response (it may have been blocked or cut off)"
                self._record_llm_failure()
                return None
            except Exception as exc:  # noqa: BLE001 - propagate as None, log details
                if provider.is_retryable(exc) and attempt < len(retry_delays):
                    logger.warning(
                        "%s request attempt %d/%d failed with a retryable error: %s",
                        provider.label,
                        attempt,
                        len(retry_delays),
                        exc,
                    )
                    continue
                logger.exception("%s request raised: %s", provider.label, exc)
                self.last_failure_reason = _classify_llm_error(exc)
                self._record_llm_failure()
                return None
        if text is None:
            self.last_failure_reason = "the model returned no response"
            self._record_llm_failure()
            return None
        logger.info("%s response: %d chars", provider.label, len(text))
        parsed = self._extract_json(text)
        if parsed is None:
            logger.error(
                "%s output was non-empty but JSON parsing failed. First 500 chars: %s",
                provider.label,
                text[:500],
            )
            self.last_failure_reason = "the model's reply was not valid JSON (it may have been cut off)"
            self._record_llm_failure()
        else:
            self._record_llm_success()
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
