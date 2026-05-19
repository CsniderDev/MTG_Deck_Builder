"""FastAPI entry point for the MTG Commander deck builder."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .models.schemas import (
    AutocompleteResponse,
    BannedListResponse,
    BuildDeckRequest,
    CommanderCompanionOptionsResponse,
    DeckResponse,
    GamechangersResponse,
    RevampDeckRequest,
)
from .services.deck_builder import DeckBuilder, DeckBuilderError
from .services.edhrec import EDHRecClient
from .services.llm import LLMService
from .services.scryfall import ScryfallClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
logging.getLogger("app").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create shared service clients for the app lifetime and close them on shutdown."""
    scryfall = ScryfallClient()
    edhrec = EDHRecClient()
    llm = LLMService()
    app.state.builder = DeckBuilder(scryfall=scryfall, edhrec=edhrec, llm=llm)
    app.state.scryfall = scryfall
    app.state.edhrec = edhrec
    app.state.llm = llm
    try:
        yield
    finally:
        await scryfall.aclose()
        await edhrec.aclose()


settings = get_settings()
app = FastAPI(title="MTG Commander Deck Builder", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, object]:
    """Report whether the API is reachable and whether Gemini is configured."""
    return {"status": "ok", "llm_enabled": app.state.llm.enabled}


@app.post("/api/decks/build", response_model=DeckResponse)
async def build_deck(request: BuildDeckRequest) -> DeckResponse:
    """Build a brand-new commander deck from a commander and optional concept prompt."""
    try:
        return await app.state.builder.build(request)
    except DeckBuilderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/decks/revamp", response_model=DeckResponse)
async def revamp_deck(request: RevampDeckRequest) -> DeckResponse:
    """Revise an existing decklist according to the user's requested changes."""
    try:
        return await app.state.builder.revamp(request)
    except DeckBuilderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/cards/autocomplete", response_model=AutocompleteResponse)
async def autocomplete_cards(
    q: str = Query(..., min_length=1, max_length=100),
    commander_only: bool = Query(True),
) -> AutocompleteResponse:
    """Return commander-name suggestions for the autocomplete widget."""
    suggestions = await app.state.scryfall.autocomplete_commanders(
        q, commander_only=commander_only
    )
    return AutocompleteResponse(suggestions=suggestions)


@app.get("/api/cards/commander-options", response_model=CommanderCompanionOptionsResponse)
async def commander_companion_options(
    q: str = Query(..., min_length=1, max_length=200),
) -> CommanderCompanionOptionsResponse:
    """Return compatible partner/background-style options for a selected commander."""
    try:
        payload = await app.state.scryfall.get_commander_companion_options(q)
    except Exception:
        payload = {"primary_name": q, "relationship": None, "secondary_kind": None, "options": []}
    return CommanderCompanionOptionsResponse.model_validate(payload)

@app.get("/api/banned", response_model=BannedListResponse)
async def get_banned_list() -> BannedListResponse:
    """Return the cached Commander banned list used by the client and prompts."""
    return BannedListResponse(banned_list=await app.state.scryfall.getBannedList())

@app.get("/api/gamechangers", response_model=GamechangersResponse)
async def get_gamechangers() -> GamechangersResponse:
    """Return the current Commander game-changer list for bracket enforcement."""
    return GamechangersResponse(gamechangers=await app.state.scryfall.getGamechangers())