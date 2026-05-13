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


class DeckCard(BaseModel):
    name: str
    count: int = 1
    category: Optional[str] = None
    scryfall_id: Optional[str] = None
    mana_cost: Optional[str] = None
    type_line: Optional[str] = None


class BuildDeckRequest(BaseModel):
    commander: str = Field(..., min_length=1, max_length=200)
    bracket: BracketLevel
    prompt: str = Field(default="", max_length=4000)


class RevampDeckRequest(BaseModel):
    commander: str
    bracket: BracketLevel
    prompt: str = ""
    previous_version: int = 1
    previous_decklist: list[DeckCard]
    change_request: str = Field(..., min_length=1, max_length=4000)


class DeckResponse(BaseModel):
    version: int
    commander: DeckCard
    bracket: BracketLevel
    bracket_description: str
    prompt: str
    decklist: list[DeckCard]
    explanation: str
    notes: list[str] = []
    source: Literal["llm", "heuristic"]


class AutocompleteResponse(BaseModel):
    suggestions: list[str]
