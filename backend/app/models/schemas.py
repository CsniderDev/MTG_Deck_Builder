from typing import Literal, Optional
from pydantic import BaseModel, Field

BracketLevel = Literal[1, 2, 3, 4, 5]

BRACKET_DESCRIPTIONS: dict[int, str] = {
    1: "Exhibition: ultra-casual, no infinite combos, no mass land destruction, "
       "no game-changers, low tutor count, slower mana.",
    2: "Core: precon-level power. No game-changers, no two-card infinite combos, "
       "limited tutors, no chained extra turns.",
    3: "Upgraded: above-precon. Up to three cards from the official game-changers "
       "list allowed, late-game two-card combos acceptable, moderate tutors.",
    4: "Optimized: high power. Any number of game-changers, fast mana, efficient "
       "tutors, and combos are fair game; not fully tuned cEDH.",
    5: "cEDH: tournament-tuned. Maximum efficiency, fast mana, strong combos, "
       "and metagame considerations expected.",
}


class ScryfallImageUris(BaseModel):
    small: Optional[str] = None
    normal: Optional[str] = None
    large: Optional[str] = None
    png: Optional[str] = None
    art_crop: Optional[str] = None
    border_crop: Optional[str] = None


class MagicCardFace(BaseModel):
    name: str
    image_uris: Optional[ScryfallImageUris] = None
    type_line: Optional[str] = None
    oracle_text: Optional[str] = None


class MagicCard(BaseModel):
    name: str
    count: int = 1
    category: Optional[str] = None
    scryfall_id: Optional[str] = None
    mana_cost: Optional[str] = None
    produced_mana: list[str] = Field(default_factory=list)
    type_line: Optional[str] = None
    oracle_text: Optional[str] = None
    price: Optional[str] = None
    colors: list[str] = Field(default_factory=list)
    color_identity: list[str] = Field(default_factory=list)
    image_uris: Optional[ScryfallImageUris] = None
    card_faces: list[MagicCardFace] = Field(default_factory=list)


class DeckSubstitution(BaseModel):
    removed: list[MagicCard] = Field(default_factory=list)
    added: list[MagicCard] = Field(default_factory=list)
    explanation: str = ""


class BuildDeckRequest(BaseModel):
    commander: str = Field(..., min_length=1, max_length=200)
    secondary_commander: Optional[str] = Field(default=None, max_length=200)
    bracket: BracketLevel
    prompt: str = Field(default="", max_length=4000)
    gamechangers: list[str] = Field(default_factory=list, max_length=500)
    banned_list: list[str] = Field(default_factory=list, max_length=500)
    turnstile_token: Optional[str] = Field(default=None, max_length=4000)


class DeckPlan(BaseModel):
    """A deck's core game plan, kept across revisions so revamps stay on theme."""

    strategy: str = Field(default="", max_length=2000)
    theme_queries: list[str] = Field(default_factory=list, max_length=4)
    # Hard cap on the 99's total price (USD), read from the concept or a change request.
    budget: Optional[float] = Field(default=None, ge=0, le=100000)
    # Scryfall searches for themes the concept rules out (kept out of candidate pools).
    avoid_queries: list[str] = Field(default_factory=list, max_length=4)
    # Cards the user restored by reverting a swap: revisions must keep them unless a
    # change request names them explicitly.
    kept_cards: list[str] = Field(default_factory=list, max_length=200)


class RevampDeckRequest(BaseModel):
    commander: str
    secondary_commander: Optional[str] = Field(default=None, max_length=200)
    bracket: BracketLevel
    prompt: str = ""
    gamechangers: list[str] = Field(default_factory=list, max_length=500)
    banned_list: list[str] = Field(default_factory=list, max_length=500)
    previous_version: int = 1
    previous_decklist: list[MagicCard] = Field(..., max_length=300)
    change_request: str = Field(..., min_length=1, max_length=4000)
    turnstile_token: Optional[str] = Field(default=None, max_length=4000)
    # The plan returned with the deck being revised; omitted for imported decks, in
    # which case the revision infers one and returns it.
    plan: Optional[DeckPlan] = None


class DeckResponse(BaseModel):
    version: int
    commander: MagicCard
    secondary_commander: Optional[MagicCard] = None
    bracket: BracketLevel
    bracket_description: str
    prompt: str
    decklist: list[MagicCard]
    explanation: str
    notes: list[str] = Field(default_factory=list)
    substitutions: list[DeckSubstitution] = Field(default_factory=list)
    source: Literal["llm", "heuristic"]
    plan: Optional[DeckPlan] = None


class AutocompleteResponse(BaseModel):
    suggestions: list[str]


class BannedListResponse(BaseModel):
    banned_list: list[str] = Field(default_factory=list)


class GamechangersResponse(BaseModel):
    gamechangers: list[str] = Field(default_factory=list)


class CommanderCompanionOptionsResponse(BaseModel):
    primary_name: str
    relationship: Optional[str] = None
    secondary_kind: Optional[Literal["commander", "background"]] = None
    options: list[str] = Field(default_factory=list)
