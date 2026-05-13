/**
 * Shared TypeScript types mirroring the Pydantic models in
 * ``backend/app/models/schemas.py``. Keep these in sync when the API contract
 * changes.
 */

export type BracketLevel = 1 | 2 | 3 | 4 | 5;

export interface DeckCard {
  name: string;
  count: number;
  category?: string | null;
  scryfall_id?: string | null;
  mana_cost?: string | null;
  type_line?: string | null;
}

export type DeckSource = 'llm' | 'heuristic';

export interface DeckResponse {
  version: number;
  commander: DeckCard;
  bracket: BracketLevel;
  bracket_description: string;
  prompt: string;
  decklist: DeckCard[];
  explanation: string;
  notes: string[];
  source: DeckSource;
}

export interface BuildDeckPayload {
  commander: string;
  bracket: BracketLevel;
  prompt: string;
}

export interface RevampDeckPayload extends BuildDeckPayload {
  previous_version: number;
  previous_decklist: DeckCard[];
  change_request: string;
}

export interface HealthStatus {
  status: string;
  llm_enabled: boolean;
}

export interface AutocompleteResponse {
  suggestions: string[];
}
