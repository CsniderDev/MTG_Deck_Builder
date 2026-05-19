/**
 * Shared TypeScript types mirroring the Pydantic models in
 * ``backend/app/models/schemas.py``. Keep these in sync when the API contract
 * changes.
 */

export type BracketLevel = 1 | 2 | 3 | 4 | 5;

export type DeckSource = 'llm' | 'heuristic';

export type DeckFormMode = 'build' | 'existing';

export interface DeckResponse {
  version: number;
  commander: MagicCard;
  bracket: BracketLevel;
  bracket_description: string;
  prompt: string;
  decklist: MagicCard[];
  explanation: string;
  notes: string[];
  substitutions: DeckSubstitution[];
  source: DeckSource;
}

export interface DeckSubstitution {
  removed: MagicCard[];
  added: MagicCard[];
  explanation: string;
}

export interface BuildDeckPayload {
  commander: string;
  bracket: BracketLevel;
  prompt: string;
  gamechangers: string[];
  banned_list: string[];
}

export interface ExistingDeckActionPayload extends BuildDeckPayload {
  decklist_text: string;
}

export interface RevampDeckPayload extends BuildDeckPayload {
  previous_version: number;
  previous_decklist: MagicCard[];
  change_request: string;
}

export interface HealthStatus {
  status: string;
  llm_enabled: boolean;
}

export interface BannedListResponse {
  banned_list: string[];
}

export interface GamechangersResponse {
  gamechangers: string[];
}

export interface AutocompleteResponse {
  suggestions: string[];
}

// types/mtg.ts

export interface ScryfallImageUris {
  small?: string;
  normal?: string;
  large?: string;
  png?: string;
  art_crop?: string;
  border_crop?: string;
}

export interface ScryfallCardFace {
  name: string;
  image_uris?: ScryfallImageUris;
  type_line?: string;
  oracle_text?: string;
}

export interface MagicCard {
  name: string;
  count: number;
  category?: string;
  scryfall_id?: string;
  type_line?: string;
  oracle_text?: string;
  price?: string;
  mana_cost?: string;
  colors?: string[];
  color_identity?: string[];
  image_uris?: ScryfallImageUris;
  card_faces?: ScryfallCardFace[];
}