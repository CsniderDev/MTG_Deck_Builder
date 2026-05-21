# TODO

## Backend / Rules / Validation

- fix issue where UB and newer cards arent being recognized as real cards
  - improve validation fallback from collection lookup to fuzzy/search-based resolution
  - match against canonical names, printed names, and face names when validating cards
  - ensure Universes Beyond and newer cards are treated as valid when Scryfall resolves them as legal
  - keep prompting explicit that UB/newer cards are valid if Commander-legal
  - add tests for UB cards, newer cards, and alternate printed names
  - likely files:
    - `backend/app/services/deck_builder.py`
    - `backend/app/services/scryfall.py`
    - `backend/app/services/llm.py`

## Frontend / UX / Presentation

- add toggle to show colorless mana for cost and production
  - add a UI toggle in `DeckStats` to include/exclude colorless mana from both charts
  - keep raw stats intact and only filter chart/legend display
  - ensure the toggle styling matches the rest of the app
  - add tests covering both toggle states
  - likely files:
    - `frontend/src/components/DeckStats.tsx`
    - `frontend/src/styles.css`

- update categories to be more concise and meaningful rather than having so many
  - define a small canonical category list for deck output
  - normalize both EDHREC and LLM categories into that canonical set
  - collapse duplicate or overlapping categories into cleaner groupings
  - update deck rendering to use the normalized category names consistently
  - add tests for category normalization and rendering
  - likely files:
    - `backend/app/services/deck_builder.py`
    - `backend/app/services/llm.py`
    - `frontend/src/components/DeckResult.tsx`

## API / Shared Data Flow

- partner commander option handling
  - show valid secondary options when commander supports partner/background-like mechanics
  - ensure the chosen secondary option is sent in build and revamp requests
  - render both command-zone cards in result header if present
  - likely files:
    - `frontend/src/components/DeckForm.tsx`
    - `frontend/src/components/CommanderAutocomplete.tsx`
    - `frontend/src/api.ts`
    - `frontend/src/App.tsx`
    - `backend/app/main.py`
    - `backend/app/services/scryfall.py`
    - `backend/app/models/schemas.py`


    # FIXED
- fix partner commander deck creation issue
  - update deck sizing logic so paired commanders/backgrounds use `98` library cards instead of `99`
  - ensure combined color identity is used when validating/building paired commander decks
  - exclude both command-zone cards from the generated decklist
  - verify revamp flow also respects paired commander rules
  - add tests for partner, background, and similar paired-commander cases
  - likely files:
    - `backend/app/services/deck_builder.py`
    - `backend/app/services/scryfall.py`
    - `backend/app/models/schemas.py`
    - `backend/app/main.py`

- move the cuts/additions section above the decklist
  - move `DeckDiff` rendering into the main result flow above the decklist area
  - keep substitutions visually compact and readable on desktop/mobile
  - ensure the revised layout still works with stats and explanation sections
  - update tests to reflect the new placement
  - likely files:
    - `frontend/src/components/DeckResult.tsx`
    - `frontend/src/components/DeckDiff.tsx`
    - `frontend/src/App.tsx`
    - `frontend/src/styles.css`

- ignore the commander when posting in an already created deck if its present in the decklist
  - strip the primary commander from pasted/imported decklists before sending revamp requests
  - also strip any secondary commander/background if present in the imported list
  - make commander-line detection tolerant of fuzzy names and full printed names
  - add a backend safety net so command-zone cards are dropped even if frontend misses them
  - likely files:
    - `frontend/src/App.tsx`
    - `backend/app/services/deck_builder.py`
