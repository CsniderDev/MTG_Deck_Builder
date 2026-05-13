# MTG Commander Deck Builder

A web app that builds Magic: The Gathering Commander (EDH) decks from a
commander name, a bracket (power level 1–5), and an optional natural-language
deck concept. It combines Scryfall (card data, legality), EDHREC (community
synergy data), and Google Gemini (LLM-guided card selection and explanations)
into a single FastAPI service exposed to a React + TypeScript frontend.

- **Backend**: Python 3.11+, FastAPI, httpx, pydantic, `google-genai` (the
  modern Gen AI SDK; `from google import genai`).
- **Frontend**: React 18 + TypeScript, Vite, Vitest.
- **External APIs**: Scryfall (`https://api.scryfall.com`), EDHREC JSON
  (`https://json.edhrec.com`), Google Gemini (`gemini-2.5-flash` by default).

---

## Table of contents

1. [Architecture](#architecture)
2. [Bracket rules and game-changers](#bracket-rules-and-game-changers)
3. [Quick start](#quick-start)
4. [Configuration](#configuration)
5. [REST API](#rest-api)
6. [Backend reference](#backend-reference)
7. [Frontend reference](#frontend-reference)
8. [Tests](#tests)
9. [Development notes](#development-notes)

---

## Architecture

```
              ┌──────────────────────────┐
              │ React + TS (Vite, 5173)  │
              │  App.tsx                 │
              │   ├─ DeckForm            │
              │   │   └─ CommanderAuto…  │
              │   └─ DeckResult          │
              │  api.ts ── fetch /api/*  │
              └────────────┬─────────────┘
                           │ JSON
              ┌────────────▼─────────────┐
              │ FastAPI (uvicorn, 8000)  │
              │  app/main.py routes      │
              │   ├─ /api/health         │
              │   ├─ /api/decks/build    │
              │   ├─ /api/decks/revamp   │
              │   └─ /api/cards/autoco…  │
              │  DeckBuilder service     │
              │   ├─ ScryfallClient      │
              │   ├─ EDHRecClient        │
              │   └─ LLMService (Gemini) │
              └────────────┬─────────────┘
                           │ HTTPS
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
    Scryfall API       EDHREC JSON        Gemini API
```

Build pipeline (`POST /api/decks/build`):

1. Resolve and validate the commander via Scryfall (`resolve_commander`).
2. Fetch EDHREC recommendations for that commander (categories of cards).
3. **If the user provided a deck concept**: call Gemini with the commander,
   bracket rules, game-changer constraints, the concept (primary direction),
   and the EDHREC pool (reference). The LLM is required — if it is disabled
   or fails, the request returns HTTP 400.
4. **If no concept was provided**: skip the LLM entirely and build a heuristic
   deck from the top EDHREC picks, respecting bracket game-changer limits.
5. Normalize the decklist: enforce singleton rules, filter excess
   game-changers, and pad with basics from the commander's color identity
   until exactly 99 cards remain.

The revamp pipeline (`POST /api/decks/revamp`) sends the previous decklist and
a free-form change request to Gemini and returns an incremented version
number. If the LLM is unavailable, the server rebuilds the deck heuristically
and tags the response with a note.

---

## Bracket rules and game-changers

Brackets are defined in `backend/app/models/schemas.py` as
`BRACKET_DESCRIPTIONS`. They are passed verbatim into the LLM prompt.

| Bracket | Name        | Game-changer limit | Notes                                       |
| ------- | ----------- | ------------------ | ------------------------------------------- |
| 1       | Exhibition  | 0                  | Ultra-casual, no infinites, no MLD.         |
| 2       | Core        | 0                  | Precon-level power. No two-card infinites.  |
| 3       | Upgraded    | 3                  | Above precon. Late-game combos acceptable.  |
| 4       | Optimized   | unrestricted       | High power, not fully tuned cEDH.           |
| 5       | cEDH        | unrestricted       | Tournament-tuned.                           |

The official game-changer list and per-bracket limits live in
`backend/app/constants.py` (`GAMECHANGERS`, `GAMECHANGER_LIMITS`,
`gamechanger_limit`). Limits are enforced in two places:

- **LLM prompt**: `_gamechanger_block` in `services/llm.py` tells Gemini the
  exact limit and lists every game-changer by name.
- **Post-generation filter**: `_enforce_gamechanger_limit` in
  `services/deck_builder.py` trims excess game-changers from whatever list is
  about to be normalized (LLM output or heuristic build) and records a note.

---

## Quick start

### Prerequisites

- Python 3.11+
- Node.js 18+
- (Optional) A Gemini API key — without one, prompt-driven builds return 400
  and prompt-less builds fall through to the heuristic.

### Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Optional: create backend/.env with GEMINI_API_KEY=...
uvicorn app.main:app --reload --port 8000
```

Or, without activating the venv, from the repo root:

```powershell
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000 --app-dir backend
```

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api/*` to <http://localhost:8000>.

### VS Code task

The workspace ships a `dev: run all` task that launches both servers in
parallel terminals. Run it via **Ctrl+Shift+P → Tasks: Run Task**.

---

## Configuration

Backend settings come from environment variables, optionally loaded from
`backend/.env`. The full schema is `Settings` in `backend/app/config.py`.

| Variable             | Default                       | Purpose                              |
| -------------------- | ----------------------------- | ------------------------------------ |
| `GEMINI_API_KEY`     | _(empty)_                     | Enables Gemini-driven builds.        |
| `GEMINI_MODEL`       | `gemini-2.5-flash`            | Any Gemini model your key can use.   |
| `FRONTEND_ORIGIN`    | `http://localhost:5173`       | CORS allow-origin.                   |
| `SCRYFALL_BASE_URL`  | `https://api.scryfall.com`    | Override for testing/mocking.        |
| `EDHREC_BASE_URL`    | `https://json.edhrec.com`     | Override for testing/mocking.        |
| `HTTP_TIMEOUT_SECONDS` | `20.0`                      | httpx timeout for outbound requests. |

---

## REST API

### `GET /api/health`

Returns `{"status": "ok", "llm_enabled": <bool>}`. `llm_enabled` is `true`
when `GEMINI_API_KEY` is set.

### `POST /api/decks/build`

Request body (`BuildDeckRequest`):

```json
{
  "commander": "Atraxa, Praetors' Voice",
  "bracket": 3,
  "prompt": "Superfriends with proliferate, avoid infinite combos."
}
```

- `commander` (1–200 chars): fuzzy-matched via Scryfall.
- `bracket`: `1 | 2 | 3 | 4 | 5`.
- `prompt` (0–4000 chars): optional concept. Non-empty values require the LLM.

Response (`DeckResponse`):

```json
{
  "version": 1,
  "commander": { "name": "Atraxa, Praetors' Voice", "count": 1, "category": "Commander" },
  "bracket": 3,
  "bracket_description": "Upgraded: above-precon ...",
  "prompt": "Superfriends with proliferate, ...",
  "decklist": [{ "name": "Sol Ring", "count": 1, "category": "Ramp" }],
  "explanation": "This deck leans into ...",
  "notes": ["Removed 1 game-changer card(s) to respect the bracket 3 limit of 3."],
  "source": "llm"
}
```

Errors return HTTP 400 with `{"detail": "..."}` (e.g. invalid commander,
prompt without LLM configured, LLM produced unparseable output).

### `POST /api/decks/revamp`

Send the previous decklist and a free-form change request to get a new
version with the requested edits applied.

```json
{
  "commander": "Atraxa, Praetors' Voice",
  "bracket": 3,
  "prompt": "Superfriends with proliferate.",
  "previous_version": 1,
  "previous_decklist": [{ "name": "Sol Ring", "count": 1 }],
  "change_request": "Cut the infinite combos and add two more wraths."
}
```

`version` in the response is `previous_version + 1`. If the LLM is disabled,
the server returns a fresh heuristic build and prepends a note.

### `GET /api/cards/autocomplete`

Query params:

- `q` (required, 1–100 chars): typed input.
- `commander_only` (default `true`): filter to Commander-legal cards.

Returns `{"suggestions": ["Atraxa, Praetors' Voice", ...]}` ordered by EDHREC
popularity (when `commander_only=true`) or Scryfall autocomplete order.

---

## Backend reference

### `backend/app/main.py`

FastAPI entry point. Wires the service clients into an
`asynccontextmanager`-based `lifespan`, registers CORS, and exposes the four
HTTP routes.

- `lifespan(app)` — startup hook that constructs a shared `ScryfallClient`,
  `EDHRecClient`, `LLMService`, and `DeckBuilder`, attaches them to
  `app.state`, and closes the HTTP clients on shutdown.
- `health()` → `GET /api/health`.
- `build_deck(request)` → `POST /api/decks/build`. Translates
  `DeckBuilderError` into HTTP 400.
- `revamp_deck(request)` → `POST /api/decks/revamp`.
- `autocomplete_cards(q, commander_only)` → `GET /api/cards/autocomplete`.

### `backend/app/config.py`

- `Settings` — `pydantic_settings.BaseSettings` subclass loading from
  environment / `backend/.env`. Fields: `gemini_api_key`, `gemini_model`,
  `frontend_origin`, `scryfall_base_url`, `edhrec_base_url`,
  `http_timeout_seconds`.
- `get_settings()` — `lru_cache`d accessor.

### `backend/app/constants.py`

- `GAMECHANGERS: frozenset[str]` — the curated WotC Commander game-changer
  list.
- `GAMECHANGER_LIMITS: dict[int, int | None]` — per-bracket caps;
  `None` means unrestricted.
- `gamechanger_limit(bracket)` — convenience lookup.

### `backend/app/models/schemas.py`

Pydantic v2 models used for request/response validation:

- `BracketLevel = Literal[1, 2, 3, 4, 5]`.
- `BRACKET_DESCRIPTIONS: dict[int, str]` — human-readable bracket text
  embedded in LLM prompts and returned in responses.
- `DeckCard` — `name`, `count`, `category`, `scryfall_id`, `mana_cost`,
  `type_line`.
- `BuildDeckRequest` — `commander` (1–200 chars), `bracket`, `prompt`
  (≤ 4000 chars).
- `RevampDeckRequest` — adds `previous_version`, `previous_decklist`, and a
  required `change_request`.
- `DeckResponse` — full build payload (see API section above).
- `AutocompleteResponse` — `{ suggestions: list[str] }`.

### `backend/app/services/scryfall.py`

Polite async wrapper over the Scryfall REST API.

- `ScryfallError` — raised on any non-success response.
- `ScryfallClient`
  - `__init__(client=None)` — uses an injected `httpx.AsyncClient` or builds
    one; tracks ownership for `aclose`.
  - `_get(path, params)` — serialized via an `asyncio.Lock` plus a ~75 ms
    delay to stay under Scryfall's 50–100 ms request guidance.
  - `named(name, fuzzy=True)` — wraps `/cards/named`.
  - `resolve_commander(name)` — fuzzy-matches a name and verifies the result
    is Commander-legal and either a legendary creature or a planeswalker
    with the "can be your commander" clause.
  - `get_card(name)` — exact match with a fuzzy fallback; returns `None` on
    failure (used during normalization).
  - `autocomplete_commanders(query, commander_only=True, limit=15)` — when
    `commander_only` is true, runs `/cards/search` with `is:commander` and
    `order=edhrec`; otherwise hits `/cards/autocomplete`. Empty/whitespace
    queries short-circuit without an HTTP call.

### `backend/app/services/edhrec.py`

Async client for EDHREC's public JSON pages.

- `EDHRecError` — raised on 404/500.
- `commander_to_slug(name)` — normalizes a commander name into EDHREC's URL
  slug (lowercase, strip apostrophes, hyphenate, take the front face of
  split/MDFC names).
- `EDHRecClient`
  - `commander_page(commander_name)` — `GET /pages/commanders/<slug>.json`.
  - `extract_recommendations(page)` (static) — flattens
    `container.json_dict.cardlists` into `{category_header: [card_dict, …]}`,
    keeping `name`, `num_decks`, `synergy`, `salt`, `label`, etc. Skips
    empty groups.

### `backend/app/services/llm.py`

Gemini wrapper. Returns `None` instead of raising when disabled, so callers
can fall back gracefully.

- `_DECK_SCHEMA_INSTRUCTIONS` — the JSON contract appended to every prompt:
  schema, singleton rule, color identity rule, "user concept is primary
  direction" rule.
- `_gamechanger_block(limit, gamechangers)` — formats the bracket-specific
  game-changer rule and the full list for the prompt.
- `LLMService`
  - `__init__()` — reads the API key; instantiates a `genai.Client` when
    `gemini_api_key` is set and logs `LLMService initialized with model=...`.
  - `enabled` — property reflecting whether a key was configured.
  - `build_deck(commander, bracket, bracket_description, user_prompt,
    recommendations, gamechanger_limit, gamechangers)` — composes the prompt
    via `_build_prompt` and parses the JSON response.
  - `revamp_deck(...)` — analogous prompt for editing an existing decklist.
  - `_build_prompt(...)` — assembles commander metadata, bracket text,
    game-changer block, the user's concept (marked as primary direction),
    and a compact EDHREC pool.
  - `_generate_json(prompt)` — calls
    `client.aio.models.generate_content(...)` with
    `GenerateContentConfig(response_mime_type="application/json")` to force
    structured output. Logs request size, response size, and on failure logs
    the `finish_reason` / `prompt_feedback` (empty output) or the first 500
    chars of unparseable text (JSON failure).
  - `_extract_json(text)` (static) — strips Markdown fences if present and
    parses the first `{...}` block; tolerant of trailing prose.

### `backend/app/services/deck_builder.py`

Orchestrates the build pipeline. Holds module-level constants `DECK_SIZE = 99`,
`_BASIC_BY_COLOR` (color → basic-land name), and `_BASIC_NAMES`.

- `DeckBuilderError` — surfaced to the HTTP layer as 400.
- `DeckBuilder`
  - `__init__(scryfall, edhrec, llm)` — stores the three clients.
  - `build(request)` — resolves the commander, fetches recommendations, then
    branches on `request.prompt`:
    - Non-empty prompt: requires `llm.enabled`; raises `DeckBuilderError` if
      Gemini is disabled or returns `None`.
    - Empty prompt: skips the LLM entirely.
    The result is handed to `_materialize`.
  - `revamp(request)` — sends the previous decklist plus the change request
    to `llm.revamp_deck`. If the LLM is unavailable, falls back to a fresh
    heuristic build and prepends an explanatory note.
  - `_resolve_commander(name)` — wraps `ScryfallError` as `DeckBuilderError`.
  - `_fetch_recommendations(name)` — wraps `EDHRecError` and returns `{}` so
    downstream code can still produce a basic-land heavy fallback.
  - `_materialize(llm_result, commander_card, recommendations, bracket)` —
    chooses between the LLM list and `_heuristic_decklist`, enforces the
    game-changer limit on the raw list, then runs `_normalize_decklist`.
    Returns `(decklist, explanation, source, notes)`.

Module-level helpers:

- `_to_card(card)` — builds the commander `DeckCard` row.
- `_normalize_decklist(raw_list, commander_card, notes)` — dedupes by
  lowercased name, enforces singleton for non-basics, drops duplicate
  commander entries, trims excess, and pads with `_pad_with_basics` until
  the deck contains exactly 99 cards.
- `_trim_to_size(deck, target)` — first drops surplus basics, then pops the
  last inserted non-basic until the count is correct.
- `_pad_with_basics(deck, commander_card, needed)` — cycles through the
  commander's color identity (or `Wastes` for colorless) to top up to 99.
- `_enforce_gamechanger_limit(raw_list, bracket, notes)` — keeps the first
  `gamechanger_limit(bracket)` game-changer entries and discards the rest,
  appending a `notes` entry.
- `_heuristic_decklist(commander_card, recommendations, bracket)` — walks
  the EDHREC categories, deduping and respecting the game-changer cap, until
  ~65 non-land picks are collected.
- `_heuristic_explanation(commander_card, recommendations)` — produces a
  short paragraph for the response when no LLM is used.

### `backend/tests/`

- `conftest.py` — adds the package root to `sys.path` and clears any leaked
  `GEMINI_API_KEY` so tests run with the LLM disabled by default.
- `test_scryfall.py` — fuzzy lookup, commander validation (legendary
  creature, planeswalker with clause, rejects illegal/non-legendary),
  autocomplete (commander-only ordering, broad mode, empty-query fast path,
  404 → empty), `get_card` fuzzy fallback.
- `test_edhrec.py` — slug normalization, page fetch + parse, 404 raises,
  empty-group filtering.
- `test_llm.py` — JSON extraction with/without code fences, malformed input
  handled gracefully, disabled service returns `None`.
- `test_deck_builder.py` — singleton enforcement, basic-land padding,
  trimming to 99, prompt-requires-LLM error, LLM-failure surfacing,
  empty-prompt skips the LLM, and the four game-changer cases (limit
  trimming, bracket 2 zero allowance, bracket 4 unrestricted, heuristic
  respecting limits).
- `test_api.py` — end-to-end FastAPI tests using `httpx.AsyncClient` against
  mocked Scryfall/EDHREC responses for every route.

### `backend/requirements.txt`

`fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `pydantic-settings`,
`python-dotenv`, `google-genai`. Dev-only extras (pytest, respx) are
installed manually; see the test files for what they import.

### `backend/pytest.ini`

Pytest config; registers `asyncio_mode = auto` so `@pytest.mark.asyncio`
tests run without further setup.

---

## Frontend reference

All sources are TypeScript. The Vitest tests still use `.jsx` extensions but
import the TS modules — `tsconfig.json` has `allowJs: true` so they coexist.

### `frontend/index.html`

Vite entry document. Loads `/src/main.tsx` as a module.

### `frontend/vite.config.js`

Vite config: `@vitejs/plugin-react`, dev server on port 5173, proxies
`/api/*` to `http://localhost:8000`.

### `frontend/vitest.config.js`

Vitest config: jsdom environment, `tests/setup.js` for `@testing-library/jest-dom`
matchers, globals enabled.

### `frontend/tsconfig.json`

TypeScript options: `strict`, `noImplicitAny`, `jsx: react-jsx`,
`moduleResolution: bundler`, `allowJs: true` (for `.jsx` tests), Vitest
globals and `jest-dom` types pre-registered. `noEmit: true` — `tsc` is used
purely as a type-checker (`npm run typecheck`).

### `frontend/package.json`

Scripts:

- `dev` — `vite` (dev server with HMR).
- `build` — `vite build`.
- `preview` — serve the production build.
- `test` / `test:watch` — Vitest.
- `typecheck` — `tsc --noEmit`.

### `frontend/src/main.tsx`

Mounts `<App />` into `#root` inside `React.StrictMode`. Throws if the root
element is missing.

### `frontend/src/types.ts`

Source of truth for shared TypeScript interfaces; mirrors the Pydantic
schema.

- `BracketLevel = 1 | 2 | 3 | 4 | 5`.
- `DeckCard`, `DeckSource = 'llm' | 'heuristic'`, `DeckResponse`.
- `BuildDeckPayload`, `RevampDeckPayload extends BuildDeckPayload`.
- `HealthStatus`, `AutocompleteResponse`.

### `frontend/src/vite-env.d.ts`

Vite client types plus a `declare module '*.css'` so side-effect CSS imports
type-check under strict TS.

### `frontend/src/api.ts`

Typed `fetch` wrapper.

- `postJson<T>(path, body)` — internal helper that POSTs JSON and surfaces
  the server's `detail` string in the thrown `Error`.
- `buildDeck(payload)` → `POST /api/decks/build`.
- `revampDeck(payload)` → `POST /api/decks/revamp`.
- `fetchHealth()` → `GET /api/health`.
- `AutocompleteOptions` — `{ signal?: AbortSignal; commanderOnly?: boolean }`.
- `autocompleteCommanders(query, options?)` — typed autocomplete fetch; the
  `signal` lets `CommanderAutocomplete` cancel in-flight requests on input
  change.

### `frontend/src/App.tsx`

Top-level component. Holds the most recent `deck`, the form state used to
issue the request, and a `health` snapshot fetched once on mount.

- `App()` — renders the header (with a warning when the LLM is disabled),
  the form, an inline error banner, and the result panel.
- `handleBuild(values)` — sets loading state, calls `buildDeck`, captures
  errors as user-facing strings.
- `handleRevamp(changeRequest)` — disabled unless a deck and form state are
  present; calls `revampDeck` with `previous_version` and
  `previous_decklist` taken from the current `DeckResponse`.

### `frontend/src/components/DeckForm.tsx`

Build form.

- `BRACKETS` — labelled options for the bracket `<select>`.
- `DeckFormProps` — `{ onSubmit, loading?, initial? }`.
- `DeckForm({ onSubmit, loading, initial })` — controlled commander/bracket/
  prompt state (`useState`). Renders `CommanderAutocomplete` for the
  commander field. `handleSubmit` trims values before invoking `onSubmit`.

### `frontend/src/components/CommanderAutocomplete.tsx`

Debounced commander typeahead with full keyboard support and ARIA combobox
semantics.

- Constants: `MIN_QUERY = 2`, `DEBOUNCE_MS = 250`.
- `CommanderAutocompleteProps` — `{ value, onChange, placeholder?, required?, disabled? }`.
- Refs: `abortRef` (current in-flight request), `debounceRef` (pending
  timer), `skipNextFetchRef` (suppresses the fetch immediately after a pick
  so the dropdown closes cleanly).
- `useEffect([value])` — debounces input, aborts the prior request, calls
  `autocompleteCommanders`, and silently swallows aborts/errors.
- `pick(name)` — commits a selection, closes the dropdown, and arms
  `skipNextFetchRef`.
- `handleKeyDown(event)` — `ArrowDown`/`ArrowUp` move the active index,
  `Enter` selects, `Escape` closes the dropdown. The combobox exposes
  `aria-expanded`, `aria-activedescendant`, `aria-controls`, and `role`
  attributes throughout.

### `frontend/src/components/DeckResult.tsx`

Read-only deck panel plus the revamp form.

- `buildDeckText(deck)` — produces a plain-text decklist grouped by category,
  used by the copy-to-clipboard button.
- `DeckResultProps` — `{ deck, onRevamp, revamping? }`.
- `DeckResult({...})` — local state for the change-request textarea and a
  transient `copied` flag. Memoizes the text export with `useMemo`. Splits
  `deck.explanation` on blank lines into paragraphs and renders categories
  with their card counts.
- `handleCopy()` — writes `decklistText` to the clipboard and shows a
  "Copied!" badge for 1.5 s.
- `handleRevampSubmit(event)` — validates the textarea, calls `onRevamp`,
  clears the input on success.

### `frontend/src/styles.css`

Global styles: layout grid for the form/result split, autocomplete dropdown,
warning/error banners. Imported once in `main.tsx`.

### `frontend/tests/`

All tests use `@testing-library/react` + `@testing-library/user-event` and
mock `fetch` globally.

- `setup.js` — registers `@testing-library/jest-dom` matchers for Vitest.
- `api.test.js` — exercises `buildDeck`, `revampDeck`, `fetchHealth`, and
  `autocompleteCommanders`, including error paths (non-OK responses, missing
  `detail`) and that the autocomplete URL is built with the right query
  string.
- `DeckForm.test.jsx` — renders the form, types into the commander field,
  changes the bracket, and asserts `onSubmit` is called with trimmed
  values and a numeric `bracket`.
- `DeckResult.test.jsx` — renders a sample deck, asserts categories render,
  the copy button writes the formatted text to the clipboard, and the
  revamp textarea calls `onRevamp` with the trimmed change request.
- `CommanderAutocomplete.test.jsx` — covers debouncing under `MIN_QUERY`,
  fetch + display, mouse selection, keyboard navigation (`ArrowDown`,
  `ArrowUp`, `Enter`), `Escape` to close, and silent error handling.
- `App.test.jsx` — integration test that mocks `fetch` for `/api/health`,
  `/api/decks/build`, and `/api/decks/revamp`, then drives the full build →
  revamp flow and asserts the version label increments to `v2`.

---

## Tests

### Backend

From the repo root (or `backend/`):

```powershell
backend\.venv\Scripts\python.exe -m pytest -q
```

### Frontend

```powershell
cd frontend
npm test          # one-shot Vitest run
npm run typecheck # tsc --noEmit, strict mode
```

---

## Development notes

- **External services are mocked in tests.** `respx` intercepts httpx calls;
  Vitest tests stub `global.fetch`. No network is touched during CI.
- **LLM is optional but enforced when used.** `LLMService.enabled` reflects
  whether `GEMINI_API_KEY` is set. Prompts require it; prompt-less builds
  short-circuit to the heuristic path.
- **Singleton + 99-card invariants** are enforced server-side regardless of
  the LLM's output. `_normalize_decklist` is the last line of defense.
- **Color identity** is read from Scryfall's `color_identity`. The basic-land
  padder uses `_BASIC_BY_COLOR` for WUBRG and `Wastes` for colorless.
- **Backgrounds / Partner pairs** are not yet supported — only single-card
  commanders resolve through `resolve_commander`.
- **Scryfall rate limit.** The client serializes requests through an
  `asyncio.Lock` with a 75 ms delay, well under Scryfall's published 50–100
  ms guidance.
- **EDHREC fallback.** If a commander has no EDHREC page, the recommendations
  dict is empty and the heuristic effectively produces a basic-land-heavy
  shell. With an LLM available, prompts still work because the EDHREC pool
  is treated as candidates, not a mandate.
