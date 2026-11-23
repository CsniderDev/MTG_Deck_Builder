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
from .budget import budget_instructions, parse_budget
from .llm_providers import LLMEmptyResponseError, LLMProvider, create_provider

logger = logging.getLogger(__name__)


_DECK_SCHEMA_INSTRUCTIONS = """
Respond ONLY with a single JSON object, no prose, no markdown fences. Do NOT return the
full decklist - return only the swaps; the server applies them to the current list. Schema:
{
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
- The deck stays at its current size (99 cards, or 98 with a partner/background): every
  swap removes exactly as many cards as it adds, and "removed" cards must come from the
  current decklist. Keep at least 35 lands - only swap a land for a land.
- Use the commander's color identity only. Never include cards outside it.
- consider the theme of the commander and the user's concept as the primary direction for card selection. 
  Use the EDHREC recommendations as a reference pool, but substitute freely to honor the concept and constraints.
- Basic lands ("Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes") may
  repeat with count > 1; every other card must have count == 1 (singleton) UNLESS the card specifies otherwise, like "Hare Apparent"
  or other similar cards.
- List every nonland card FIRST, then the lands, so the mana base is chosen after the
  spells are known.
- The mana base must match the deck's color requirements. Before choosing lands, tally
  the colored mana symbols in the mana costs of the nonland cards and the commander
  (count the commander's symbols double - it is cast repeatedly; a hybrid symbol like
  {W/U} counts half for each color). Each color's share of colored mana SOURCES should
  roughly match its share of those symbols.
- Count every colored source, not just basics: nonbasic lands count once for each color
  they produce (fetch lands count for each basic type they can find), and mana rocks
  and mana creatures (e.g. Arcane Signet, Talismans, Llanowar Elves, Birds of Paradise)
  count about half a source per color - they cost a card and a turn and can be
  removed. Sol Ring and other colorless rocks, and one-shot rituals, are not colored
  sources.
- Fixing REPLACES basics; it does not add to the land count. Keep the total land count
  normal: at least 35 lands, usually 35-38 depending on the curve. Count modal double-faced cards with a land side toward it.
- Choose the nonbasic lands and mana rocks/dorks first, then use basics to fill
  whatever colors are still short. Example: symbols that are 25% W, 10% U, 15% B,
  40% R, 10% G. With no other fixing, 20 basics split about 5 Plains, 2 Island,
  3 Swamp, 8 Mountain, 2 Forest. But if the duals and rocks already give green and
  blue plenty of sources while red has few, shift the basics further toward red (and
  white) so the TOTAL sources per color land near 25/10/15/40/10.
- Colors whose cards are cheap or appear in early turns need more sources than their
  raw share suggests; give every color the deck actually uses at least a few sources.
  Do not add basics of a color the deck's cards don't use.
- Cover ramp, card draw, removal/interaction, threats, and an appropriate land
  count for the bracket and curve.
- Respect the bracket constraints described in the user message, including the
  game-changer limit. Treat the user's deck concept as the primary direction
  for card selection - EDHREC recommendations are a candidate pool, not a
  mandate. Substitute freely to honor the concept.
- When building from scratch, return an empty ``substitutions`` array.
- When revising an existing deck, include a ``substitutions`` entry for each
  meaningful card-for-card or package-for-package change, with concise reasons.
- Cards already in the user's deck are valid cards.
- If there is card data provided in the attached JSON payload, it is verified, real, and exists on Scryfall. 
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
- Cards from sets newer than your training data are real; judge any card you don't
  recognize by the rules text provided, not by whether you have heard of it.
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


def _compact_decklist_for_prompt(
    previous_decklist: list[dict[str, Any]], include_prices: bool = False
) -> list[dict[str, Any]]:
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
        if include_prices and card.get("price"):
            compact_card["usd"] = str(card.get("price"))
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


def _budget_block(user_prompt: Optional[str]) -> str:
    """Budget instructions for a budget stated in free text (empty when none)."""
    return budget_instructions(parse_budget(user_prompt))


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

    async def plan_deck(
        self,
        commander: dict[str, Any],
        bracket: int,
        bracket_description: str,
        user_prompt: str,
        gamechanger_limit: Optional[int] = None,
        budget: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Step 1 of a build: strategy, role counts, and Scryfall theme searches.

        A user concept is a hard requirement: the theme searches implement it, and the
        commander's other popular archetypes are listed as ``avoid_queries`` so the pool
        doesn't drift back to the commander's default build.
        """
        if self._provider is None:
            return None
        ci = "".join(commander.get("color_identity") or []) or "C"
        gc_rule = (
            "unlimited" if gamechanger_limit is None else f"at most {gamechanger_limit}"
        )
        concept = (user_prompt or "").strip()
        prompt = (
            "You are an expert Magic: The Gathering Commander deck builder planning a deck.\n\n"
            f"Commander: {commander.get('name')}\n"
            f"Type line: {commander.get('type_line')}\n"
            f"Color identity: {ci}\n"
            f"Oracle text: {commander.get('oracle_text') or '(none)'}\n\n"
            f"Bracket {bracket}: {bracket_description} Game-changers allowed: {gc_rule}.\n"
            f"{budget_instructions(budget)}"
            + (
                f"USER'S DECK CONCEPT (a hard requirement, not a suggestion): {concept}\n\n"
                if concept
                else "No concept given - build around the commander's strengths.\n\n"
            )
            + "Plan the deck. Respond ONLY with a JSON object:\n"
            "{\n"
            '  "strategy": "<2-3 sentences: the user\'s concept as the game plan, and how it wins>",\n'
            '  "targets": {"lands": <35-40>, "ramp": <int>, "draw": <int>, "removal": <int>, "wipe": <int>},\n'
            '  "theme_queries": ["<Scryfall search query>", "..."],\n'
            '  "avoid_queries": ["<Scryfall search query for an off-concept theme>", "..."]\n'
            "}\n"
            "Rules:\n"
            "- The concept decides what the deck is. If it names a specific or niche plan (e.g. "
            "\"only clones and copies of the commander\", \"strictly enchantments\"), build THAT "
            "plan and nothing else - even if the commander is usually built another way. Use "
            "the commander's other abilities only where they directly help the concept.\n"
            "- strategy: restate the concept faithfully. Do not add themes the user didn't ask "
            "for (tribal types, counters, tokens...) unless they are needed to make the concept work.\n"
            "- targets are card counts for a 99-card deck; cards for the theme fill the rest. "
            "Lands: at least 35 (35-38 typical). Typical: 8-12 ramp, 8-12 draw, 6-10 removal/interaction, 1-4 board wipes. "
            "Adjust for the bracket, curve and concept.\n"
            "- theme_queries: 2-4 Scryfall search queries, EACH of which finds cards that directly "
            "carry out the concept (or, with no concept, the commander's main plan), using "
            "Scryfall syntax such as o:\"text\" (rules text), t:type, kw:keyword, otag:tag, "
            "cmc<=3, pow>=4. Examples: o:proliferate, o:\"+1/+1 counter\" t:creature, t:goblin, "
            "otag:clone, o:\"token that's a copy\", otag:sacrifice-outlet. Never add a query for a "
            "theme the concept doesn't call for.\n"
            "- avoid_queries: 0-3 Scryfall queries for the themes this commander is commonly "
            "built around that the user's concept does NOT ask for (e.g. for a clone-focused "
            "deck of a commander that cares about Gods: t:god). Cards they find are removed "
            "from the candidate pool unless a concept search also finds them. Leave it empty "
            "when there is no concept or the concept is the commander's usual plan.\n"
            "- Do NOT add color, format, legality or ordering filters to any query - the server "
            "adds those. Keep each query short and specific.\n"
        )
        return await self._generate_json(prompt)

    async def select_cards(
        self,
        commander: dict[str, Any],
        bracket: int,
        bracket_description: str,
        user_prompt: str,
        plan: dict[str, Any],
        targets: dict[str, int],
        pool_text: str,
        deck_size: int,
        gamechanger_limit: Optional[int] = None,
        budget: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """Step 2 of a build: choose cards from the numbered candidate pool."""
        if self._provider is None:
            return None
        ci = "".join(commander.get("color_identity") or []) or "C"
        nonland = deck_size - targets["lands"]
        gc_rule = (
            "Game-changers (tagged [game changer]) are unrestricted."
            if gamechanger_limit is None
            else f"Pick at most {gamechanger_limit} card(s) tagged [game changer]."
        )
        prompt = (
            "You are an expert Magic: The Gathering Commander deck builder. Choose the cards "
            "for this deck from the numbered candidate pool below.\n\n"
            f"Commander: {commander.get('name')} (color identity {ci})\n"
            f"Oracle text: {commander.get('oracle_text') or '(none)'}\n"
            f"Bracket {bracket}: {bracket_description}\n"
            f"{budget_instructions(budget)}"
            f"USER'S DECK CONCEPT (a hard requirement): {user_prompt or '(none)'}\n"
            f"Plan: {plan.get('strategy') or '(none)'}\n"
            f"Targets: {nonland} nonland cards - including about {targets['ramp']} ramp, "
            f"{targets['draw']} card draw, {targets['removal']} removal/interaction, "
            f"{targets['wipe']} board wipes, and the rest for the theme - plus "
            f"{targets['lands']} lands in total.\n\n"
            "Respond ONLY with a JSON object:\n"
            "{\n"
            '  "picks": {\n'
            f'    "ramp": [<{targets["ramp"]} pool numbers>],\n'
            f'    "draw": [<{targets["draw"]} pool numbers>],\n'
            f'    "removal": [<{targets["removal"]} pool numbers>],\n'
            f'    "wipe": [<{targets["wipe"]} pool numbers>],\n'
            f'    "theme": [<{targets["theme"]} pool numbers>],\n'
            '    "lands": [<nonbasic land pool numbers>]\n'
            "  },\n"
            '  "explanation": "<2-4 paragraph plain-text explanation of the deck plan>",\n'
            '  "notes": ["<optional short bullet>", "..."]\n'
            "}\n"
            "Rules:\n"
            "- Fill each group with EXACTLY the number shown (they add up to "
            f"{nonland} nonland cards), each list MOST IMPORTANT FIRST, using pool numbers "
            "(e.g. 12, not \"#12\" or a name). A card goes in only one group. \"theme\" holds "
            "the creatures, synergy pieces and win conditions that make this deck what it is.\n"
            "- Follow the user's concept strictly. Every \"theme\" pick must directly carry out "
            "the concept - take them from the Theme section and [on-theme] cards first. Never "
            "fill theme slots with the commander's other popular archetypes (other tribes, "
            "counters, tokens...) that the concept didn't ask for; a Synergy-section card only "
            "belongs if it serves the concept. Where a role card (ramp, draw, removal) that "
            "also fits the concept exists, prefer it over a generic one.\n"
            "- Basic lands are added automatically - do not pick them - and the server balances "
            "their colors.\n"
            "- Only pick numbers from the pool. Every card in it is verified Commander-legal and "
            "in the deck's colors.\n"
            f"- {gc_rule}\n"
            "- Choose the best card for each job regardless of popularity. Cards tagged [new] are "
            "recent releases and [gem] are theme cards few people play - judge them by their "
            "rules text. The user wants decks that explore beyond the usual staples: include at "
            "least 6 cards tagged [new] or [gem] that genuinely fit the concept (more if they are "
            "as good as the staples), but never at the cost of the deck's core ramp, draw and "
            "interaction, and never an off-concept card just to reach the count.\n"
            "- Nonbasic lands: pick enough color fixing for the deck's colors (more for 3+ colors), "
            "weighting colors by how many mana symbols the spells need, plus a few utility lands "
            f"that fit the plan - leaving room for basics within the {targets['lands']} lands.\n"
            "- Keep a sensible mana curve for the bracket.\n"
            + (
                "- Stay within the HARD BUDGET above: add up the prices of your picks (basics cost "
                "about $0.30 each) and leave a margin. Do not state a total in your notes - the "
                "server reports the real one.\n"
                if budget is not None
                else ""
            )
            + "\n"
            "Candidate pool (#number name cost | type [$price] [tags] :: rules text for less-known cards):\n"
            f"{pool_text}\n"
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
        plan_strategy: str = "",
        theme_candidates: str = "",
        budget: Optional[float] = None,
        current_total: Optional[float] = None,
        avoid_queries: Optional[Iterable[str]] = None,
        locked_cards: Optional[Iterable[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """Ask the LLM to revise an existing decklist and explain each substitution.

        The deck's recorded game plan leads the prompt and every change must serve it
        unless the change request explicitly asks for a different plan.
        """
        if self._provider is None:
            return None
        gc_block = _gamechanger_block(gamechanger_limit, gamechangers)
        compact_previous_decklist = _compact_decklist_for_prompt(
            previous_decklist, include_prices=budget is not None
        )
        locked = [name for name in (locked_cards or []) if name]
        locked_line = (
            "KEEP THESE CARDS - the user restored them by reverting earlier swaps, so never put "
            f"them in a \"removed\" list: {', '.join(locked)}\n"
            if locked
            else ""
        )
        avoid = [q for q in (avoid_queries or []) if q]
        avoid_line = (
            "Off-concept themes - do NOT add cards whose main purpose is any of these (Scryfall "
            f"searches): {'; '.join(avoid)}\n"
            if avoid
            else ""
        )
        plan_line = plan_strategy.strip() or (
            "(not recorded - work it out from the commander, the original concept and the "
            "current list, then keep to it)"
        )
        candidates_block = (
            "CARDS YOU MAY ADD - on-theme candidates that are NOT in the deck (verified "
            "Commander-legal and in the deck's colors). Use these only in \"added\" lists; "
            "never list them as \"removed\":\n"
            f"{theme_candidates}\n\n"
            if theme_candidates
            else ""
        )

        prompt = (
            f"You are revising an existing Magic: The Gathering Commander deck.\n"
            f"Commander: {commander.get('name')} "
            f"(color identity: {''.join(commander.get('color_identity') or []) or 'C'}).\n"
            f"Bracket {bracket} - {bracket_description}\n"
            f"CORE GAME PLAN (keep it unless the change request explicitly asks to change it): {plan_line}\n"
            f"Original user concept (a hard requirement): {user_prompt or '(none)'}\n"
            f"{avoid_line}"
            f"{locked_line}"
            f"{budget_instructions(budget, current_total)}"
            f"User change request: {change_request}\n\n"
            "How to revise:\n"
            "- Apply the change request IN SERVICE OF the core game plan. Every card you add "
            "must support that plan or do a job the request asks for in a way that fits it.\n"
            "- For generic requests (\"more removal\", \"lower the curve\", \"more draw\"), choose "
            "cards that do the job AND fit the theme where possible, and cut the least "
            "on-plan cards first. Never swap theme pieces or synergy cards for generic staples "
            "unless the request asks for that.\n"
            "- Change only what the request needs; keep everything else.\n"
            "- Never add cards for a theme the concept didn't ask for (other tribes, counters, "
            "tokens...) just because the commander is popular with it.\n"
            "- Every card in a substitution's \"removed\" list MUST be copied exactly from the "
            "current decklist below - never remove a card the deck doesn't have. Each swap "
            "removes as many cards as it adds, so the deck stays the same size. List EVERY "
            "change as a swap - the server applies exactly your swaps and nothing else.\n"
            "- Only change the game plan when the request clearly asks for it (e.g. \"switch "
            "to...\", \"make it a ... deck instead\", \"change the theme/strategy\").\n\n"
            f"{candidates_block}"
            f"Here is a current list of banned cards. DO NOT include any of these cards at all:\n{json.dumps(list(banned_list or []))}\n\n"
            f"{gc_block}"
            "CURRENT DECKLIST - the ONLY cards that can appear in \"removed\" lists (compact "
            f"JSON):\n{json.dumps(compact_previous_decklist)}\n\n"
            f"Apply the change request as a list of swaps, keeping the deck coherent and "
            f"legal. For every swap, include "
            f"a substitution entry listing the removed card(s), the added card(s), and "
            f"a concise explanation of why the substitution helps. Treat Universes Beyond "
            f"cards and alternate printed or reskinned names as valid whenever Scryfall "
            f"resolves them to Commander-legal cards. Prefer official Scryfall card names "
            f"in the final JSON when possible.\n\n"
            f"{_DECK_SCHEMA_INSTRUCTIONS}\n"
            "Also include in the JSON object:\n"
            '  "plan": {"changed": <true only if the change request explicitly changed the game '
            'plan>, "strategy": "<the deck\'s core game plan after this revision, 1-3 '
            'sentences>", "theme_queries": ["<only when changed: 2-4 Scryfall queries for the '
            'new theme, e.g. o:proliferate>"], "avoid_queries": ["<only when changed: 0-3 '
            'Scryfall queries for themes the new plan rules out>"]}'
        )
        return await self._generate_json(prompt)

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
