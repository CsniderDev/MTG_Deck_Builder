# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A web app that builds Magic: The Gathering Commander (EDH) decks from a commander name, an optional secondary
commander/background, a bracket (power level 1-5), and an optional natural-language deck concept. A FastAPI
backend combines Scryfall (card data/legality/prices), EDHREC (community synergy data), and an LLM — Google Gemini or
OpenAI/ChatGPT, chosen by `LLM_PROVIDER` — (LLM-guided card selection) behind a React + TypeScript (Vite) frontend.

- **Backend**: Python 3.11+, FastAPI, httpx, pydantic v2, `google-genai` and `openai` SDKs.
- **Frontend**: React 18 + TypeScript, Vite, Vitest, Recharts (mana pie charts).
- **External APIs**: Scryfall (`https://api.scryfall.com`), EDHREC JSON (`https://json.edhrec.com`), Google
  Gemini (`gemini-2.5-flash` by default) or OpenAI (`gpt-4.1-mini` by default) — see "LLM providers" below.

`README.md` documents the original single-commander build/revamp flow in detail (API shapes, bracket table,
per-file reference) and is still broadly accurate, but the code has since grown partner/background commanders,
a live banned-list/game-changers fetch, an "existing decklist" revamp workflow, LLM substitution tracking, and
(most recently) a layer of abuse/cost protection — none of which are in the README yet. Treat the README as a
good primer and this file plus the source as the source of truth for anything that conflicts. See `SECURITY.md`
for the full rundown of the protections below plus the manual deployment steps they don't cover (Cloudflare,
Gemini budget alerts, HTTPS).

## Commands

### Backend (run from `backend/`, or prefix with `backend\.venv\Scripts\python.exe -m ... --app-dir backend`)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# also install dev-only test deps manually: pip install pytest pytest-asyncio respx
uvicorn app.main:app --reload --port 8000
```

Run tests:

```powershell
backend\.venv\Scripts\python.exe -m pytest -q
backend\.venv\Scripts\python.exe -m pytest tests/test_deck_builder.py -q          # one file
backend\.venv\Scripts\python.exe -m pytest tests/test_llm.py::test_name -q        # one test
```

`pytest.ini` sets `asyncio_mode = auto`, so async tests need no extra markers. `tests/conftest.py` clears any
leaked `GEMINI_API_KEY`/`OPENAI_API_KEY` so the LLM is disabled by default in tests; external HTTP is mocked with `respx`
(backend) — no network is touched during tests.

Requires an optional `backend/.env` with `GEMINI_API_KEY=...` (or `LLM_PROVIDER=openai` +
`OPENAI_API_KEY=...`) for LLM-guided builds; without it, prompt-driven
builds return HTTP 400 and prompt-less builds fall back to the heuristic.

### Frontend (run from `frontend/`)

```powershell
npm install
npm run dev         # Vite dev server on :5173, proxies /api/* to :8000
npm run build
npm test            # one-shot Vitest run
npm run test:watch
npm run typecheck   # tsc --noEmit, strict mode
```

To run a single frontend test file/case, use Vitest's own filters, e.g. `npx vitest run tests/DeckResult.test.jsx`
or `npx vitest run -t "test name"`.

Vitest tests use `.jsx` file extensions but import the TS modules directly (`tsconfig.json` has `allowJs: true`
so they coexist). All app source is TypeScript.

### VS Code

A `dev: run all` task launches both servers in parallel terminals (Ctrl+Shift+P → Tasks: Run Task).

## Architecture

```
React + TS (Vite, 5173)              FastAPI (uvicorn, 8000)
  App.tsx                              app/main.py routes
   +- workflow switch: build vs         +- /api/health
   |  existing-decklist                 +- /api/decks/build
   +- DeckForm                          +- /api/decks/revamp
   |   +- CommanderAutocomplete         +- /api/cards/autocomplete
   +- DeckResult                        +- /api/cards/commander-options
       +- DeckStats (recharts)          +- /api/banned
       +- DeckDiff (substitutions)      +- /api/gamechangers
  api.ts -- fetch /api/*              DeckBuilder service
                                         +- ScryfallClient
                                         +- EDHRecClient
                                         +- LLMService -> provider (Gemini | OpenAI)
                     |
        Scryfall API | EDHREC JSON | Gemini / OpenAI API
```

### Two build entry points

- **`POST /api/decks/build`** — build from scratch given a commander (+ optional secondary commander/background)
  and bracket. If a `prompt` (deck concept) is present, the LLM is required (400 if disabled/fails); otherwise
  the heuristic path runs.
- **`POST /api/decks/revamp`** — takes a `previous_decklist` plus a `change_request` and asks Gemini to edit it,
  returning `previous_version + 1` (or the same version if the decklist didn't actually change, per
  `_decklist_signature`). This endpoint is reused by the frontend's "start from existing decklist" workflow
  (`App.tsx` → `handleExistingDeckAction`), which parses a pasted plain-text decklist into `MagicCard[]` and
  calls `revamp` with `previous_version: 0`.

Both flows funnel through `DeckBuilder._materialize`, which normalizes whichever raw list won (LLM or
heuristic) via `_normalize_decklist` — enforcing singleton rules, exactly 99 cards (98 when a secondary
commander/partner occupies a second command-zone slot: `DECK_SIZE_WITH_PARTNER`), and mana-weighted basic-land
padding (`_pad_with_basics` tallies colored pips across the deck's `mana_cost` strings, then fills basics
proportionally instead of an even WUBRG split).

### Partner / background commanders

`ScryfallClient.get_commander_companion_options` inspects a commander's oracle text (`_commander_pairing_mode`)
to classify it as `partner_with` (named partner), `background` (Choose a Background), `doctor` (Doctor's
companion), `partner` (plain Partner keyword), or a partner variant (e.g. "Partner — Father & Son"), then
searches Scryfall for the legal companion set. `DeckBuilder._resolve_secondary_commander` validates a requested
secondary against that option list before `_combine_command_zone` merges both cards into a single synthetic
`command_zone` dict (merged name/type/oracle text/color identity) that gets passed to EDHREC and the LLM prompt
as if it were one commander.

### Banned list / game-changers are fetched live, not just static

`backend/app/constants.py` still holds a static `GAMECHANGERS` frozenset and `GAMECHANGER_LIMITS` used as a
fallback, but the frontend now calls `GET /api/banned` and `GET /api/gamechangers` on load
(`fetchBannedListAndGamechangers` in `api.ts`, called from `App.tsx`), which hit `ScryfallClient.getBannedList`
/ `getGamechangers` — live Scryfall searches (`banned:commander`, `is:gamechanger`) cached in-memory on the
client instance for the process lifetime. Those lists are round-tripped back to the backend on every
build/revamp request (`gamechangers`, `banned_list` fields on `BuildDeckRequest`/`RevampDeckRequest`) and used
by `_enforce_gamechanger_limit` and the LLM prompt (`_gamechanger_block` in `services/llm.py`) in preference to
the static constants. The bracket → game-changer-limit table itself (0/0/3/unrestricted/unrestricted) still
lives in `constants.GAMECHANGER_LIMITS` and is unaffected by this.

### LLM output validation and retry

After an LLM build/revamp, `DeckBuilder.validate_decklist` batch-fetches every proposed card via
`ScryfallClient.get_collection` and checks Commander legality plus color-identity legality against the command
zone. In `build()` (not `revamp()`), if any cards come back invalid the LLM is called a **second time** with
the same prompt before falling through to materialization — there's no feedback loop telling the LLM which
cards were rejected, it's a blind retry. Valid cards resolved this way are threaded through as `prefetched_cards`
so `_hydrate_cards` doesn't re-fetch them.

### Substitutions (revamp diff)

When Gemini revamps a deck it can return a `substitutions` array (`removed`/`added` card lists + explanation)
alongside the new decklist; `_normalize_substitutions` parses this and `DeckDiff.tsx` renders each swap with
card art and a "Revert this swap" button. Reverting is purely client-side: `App.tsx`'s
`applySubstitutionRevert` mutates the local `decklist` (restoring removed cards from
`previousLibraryForCurrentDeck`, decrementing/removing added ones) without another network call — it does not
ask the LLM to redo anything, so it can drift from what the server would produce if asked to revamp again.

### Deck building pipeline (build only)

`DeckBuilder.build` no longer lets the LLM write card names from memory:

1. **Plan** (`LLMService.plan_deck`, skipped without a concept): strategy, role targets
   (lands/ramp/draw/removal/wipe) and 2-4 Scryfall theme queries. Runs concurrently with the
   EDHREC fetch and `prefetch_base_searches`.
2. **Pool** (`services/card_pool.py: build_card_pool`): numbered candidates from the EDHREC
   commander page, Scryfall role searches (`otag:ramp` etc., by EDHREC rank), the plan's theme
   queries (popular + `new:card` newest-first), and the newest cards in the identity. Every card is
   checked for Commander legality, identity, the banned list and game-changer status; tags `new`
   (first printed in the last ~18 months) and `gem` (theme card not on the EDHREC page, rarely played).
   Theme queries are sanitized (`sanitize_theme_queries`) so they can't widen identity/format.
3. **Select** (`LLMService.select_cards`): the model returns pool numbers grouped by role.
4. **Assemble** (`assemble_deck`): exact deck size, role targets protected when the model
   over-picks, gaps filled from the best-ranked pool cards, game-changer limit enforced, basics
   added; then `_enforce_land_count` and `_balance_basic_lands` run as before.

Without a concept the same pool is assembled with default targets (`source="heuristic"`).
If the plan has no usable theme queries, `fallback_theme_queries` derives them from the concept text.

**Plans persist across revisions.** Builds return `DeckResponse.plan` (`DeckPlan`: strategy + theme
queries); the frontend sends it back as `RevampDeckRequest.plan`. Revamp puts the plan first in the
prompt, re-runs its theme queries to offer on-theme candidates (`render_theme_candidates`), and keeps
the plan unless the LLM reports `plan.changed` (only for requests that explicitly change the game plan;
`_next_plan`). Imported decks with no plan adopt the one the first revision infers.
Revamp still uses the name-based single-call flow, but now drops cards that fail validation.
Scryfall requests are spaced 500ms apart for search/named/collection endpoints (100ms for others)
with 429 retry/backoff - bursts above that earned 16-60s penalties.

### LLM providers

`LLMService` (`backend/app/services/llm.py`) owns the prompts, retries, JSON extraction, daily call budget and
circuit breaker; the vendor call itself is delegated to an adapter in `backend/app/services/llm_providers.py`
(`GeminiProvider` / `OpenAIProvider`, built by `create_provider` from `LLM_PROVIDER`). An adapter only has to
implement `generate_json_text(prompt)` (JSON mode, raise `LLMEmptyResponseError` on an empty reply) and
`is_retryable(exc)`. If the selected provider has no API key — or `LLM_PROVIDER` is unrecognised — the
service is disabled and the app behaves exactly as it does with no key. Adding another vendor means one new
adapter class plus a branch in `create_provider`. `GET /api/health` reports the active `llm_provider` label,
which the frontend header displays.

### Abuse / cost protection layer

`backend/app/main.py` wraps `build_deck`/`revamp_deck` in two independent layers of defense against
someone scripting requests against the Gemini-calling endpoints (full detail in `SECURITY.md`):

- **Per-IP rate limiting** via `slowapi` — a `Limiter` on `app.state.limiter` with a global
  `60/minute` default, plus `5/minute` and `{settings.daily_ip_limit}/day` (default 50) specifically on
  build/revamp. Both route functions take a `request: Request` parameter (required by `slowapi`'s
  decorator) alongside the pydantic body, which is now named `payload` rather than `request` to avoid
  the name clash.
- **A shared daily LLM call budget and circuit breaker**, both live inside `LLMService`
  (`_check_and_increment_budget`, `_circuit_open`) and gate every real call in `_generate_json` — once
  either trips, `_generate_json` returns `None` without hitting the network, and the deck builder falls
  back to the heuristic path exactly as it does when the LLM is simply disabled. `GET /api/health` exposes
  `llm_calls_today`/`llm_daily_call_limit` so usage is visible without reading logs.

`DeckBuilder` also caches full `build()`/`revamp()` responses for `settings.response_cache_ttl_seconds`
(default 120s) keyed by a hash of the request's meaningful fields (`_build_cache_key`/`_revamp_cache_key`),
so identical repeat requests — double-clicks, naive retry scripts — don't re-hit Gemini/Scryfall/EDHREC at
all. `BuildDeckRequest`/`RevampDeckRequest` also carry an optional `turnstile_token` field; when
`TURNSTILE_SECRET_KEY` is set, both routes verify it against Cloudflare's siteverify endpoint before doing
any work, and reject with 400 if it's missing/invalid. All of this is off by default (rate limiting aside)
and safe for local dev — nothing here requires configuration to keep working.

### Card hydration

Deck cards start as bare `{name, count, category}` from the LLM/heuristic. `DeckBuilder._hydrate_cards` fills
in `scryfall_id`, `mana_cost`, `produced_mana`, `type_line`, `oracle_text`, `price` (USD), `colors`,
`color_identity`, `image_uris`, and `card_faces` (for DFCs) via `ScryfallClient.get_collection`, with per-name
fuzzy fallback for anything the bulk collection endpoint misses. `DeckStats.tsx` and `MagicCard.tsx` on the
frontend depend on these hydrated fields (mana pie charts, price total, hover card preview via
`createPortal`).

### Frontend state shape

`App.tsx` is the single source of truth: it tracks the current `DeckResponse`, which of the two workflows is
active (`build` vs `existing`), the form values used for the last request (so a revamp can resubmit the same
commander/bracket/prompt), and `previousLibraryForCurrentDeck` (a snapshot of the decklist before the last
revamp, used only for substitution reverts). `types.ts` mirrors `backend/app/models/schemas.py` — keep them in
sync when the API contract changes.

## Notes for future changes

- Server-side singleton/deck-size enforcement is the last line of defense regardless of what the LLM/heuristic
  produces — don't rely on prompt instructions alone.
- Scryfall requests are serialized through an `asyncio.Lock` with a ~75ms delay per call
  (`ScryfallClient._get`/`_post`); batch through `get_collection` instead of looping `get_card` when fetching
  many cards.
- The banned-list/game-changers Scryfall caches live on the `ScryfallClient` instance, which is constructed
  once in `main.py`'s `lifespan` and shared for the process lifetime — restart the backend to pick up changes
  to either list.
- `main.py`'s `limiter` is a module-level singleton, so its rate-limit storage persists across requests within
  one process (as intended) but also across `TestClient` instances within one pytest run. `tests/test_api.py`'s
  `client` fixture calls `main_module.limiter.reset()` before each test for this reason — keep that if you add
  more API tests that hit `build`/`revamp` repeatedly.
- See `TODO.md` for known gaps (e.g., revamp not yet persisting the true previous decklist across turns, mana
  base heuristic still being refined).
