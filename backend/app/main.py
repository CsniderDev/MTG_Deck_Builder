"""FastAPI entry point for the MTG Commander deck builder."""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

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
logger = logging.getLogger(__name__)


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


def _client_ip_key(request: Request) -> str:
    """Resolve the rate-limit identity for a request.

    Only trusts the client-supplied ``X-Forwarded-For`` header when
    ``TRUST_X_FORWARDED_FOR`` is explicitly enabled, which should only happen
    when this app sits behind a proxy/CDN that overwrites that header itself
    (Cloudflare, nginx, etc). Trusting it without a proxy in front would let
    any client spoof its rate-limit identity and bypass the limits below.
    """
    if settings.trust_x_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip_key, default_limits=["60/minute"])
_BUILD_MINUTE_LIMIT = "5/minute"
_BUILD_DAILY_LIMIT = f"{settings.daily_ip_limit}/day"

app = FastAPI(title="MTG Commander Deck Builder", version="0.1.0", lifespan=lifespan)
app.state.limiter = limiter


async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a JSON error matching the rest of the API's {"detail": ...} error shape."""
    logger.warning(
        "Rate limit exceeded for %s on %s: %s", _client_ip_key(request), request.url.path, exc.detail
    )
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded ({exc.detail}). Please slow down and try again later."},
    )


app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Attach baseline hardening headers to every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


def _require_access_code(request: Request) -> None:
    """Reject LLM-triggering requests without the shared access code (when one is configured)."""
    expected = settings.access_code
    if not expected:
        return
    provided = request.headers.get("x-access-code", "")
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        logger.warning("Rejected request with a missing/wrong access code from %s", _client_ip_key(request))
        raise HTTPException(status_code=401, detail="Access code required. Ask the site owner for it.")


_TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def _verify_turnstile_token(token: Optional[str], remote_ip: str) -> bool:
    """Verify a Cloudflare Turnstile token; a no-op that always passes when unconfigured."""
    secret = settings.turnstile_secret_key
    if not secret:
        return True
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                _TURNSTILE_VERIFY_URL,
                data={"secret": secret, "response": token, "remoteip": remote_ip},
            )
        result = response.json()
    except Exception as exc:  # noqa: BLE001 - verification failures must not crash the request
        logger.warning("Turnstile verification request failed: %s", exc)
        return False
    return bool(result.get("success"))


@app.get("/api/health")
async def health() -> dict[str, object]:
    """Report whether the API is reachable and which LLM provider (if any) is configured."""
    return {
        "status": "ok",
        "llm_enabled": app.state.llm.enabled,
        "llm_provider": getattr(app.state.llm, "provider_label", None),
        "llm_calls_today": getattr(app.state.llm, "calls_today", None),
        "llm_daily_call_limit": getattr(app.state.llm, "daily_call_limit", None),
        "access_code_required": bool(settings.access_code),
    }


@app.post("/api/decks/build", response_model=DeckResponse)
@limiter.limit(_BUILD_MINUTE_LIMIT)
@limiter.limit(_BUILD_DAILY_LIMIT)
async def build_deck(request: Request, payload: BuildDeckRequest) -> DeckResponse:
    """Build a brand-new commander deck from a commander and optional concept prompt."""
    _require_access_code(request)
    if not await _verify_turnstile_token(payload.turnstile_token, _client_ip_key(request)):
        raise HTTPException(status_code=400, detail="Human verification failed. Please retry.")
    try:
        return await app.state.builder.build(payload)
    except DeckBuilderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/decks/revamp", response_model=DeckResponse)
@limiter.limit(_BUILD_MINUTE_LIMIT)
@limiter.limit(_BUILD_DAILY_LIMIT)
async def revamp_deck(request: Request, payload: RevampDeckRequest) -> DeckResponse:
    """Revise an existing decklist according to the user's requested changes."""
    _require_access_code(request)
    if not await _verify_turnstile_token(payload.turnstile_token, _client_ip_key(request)):
        raise HTTPException(status_code=400, detail="Human verification failed. Please retry.")
    try:
        return await app.state.builder.revamp(payload)
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