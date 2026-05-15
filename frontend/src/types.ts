/**
 * Shared TypeScript types mirroring the Pydantic models in
 * ``backend/app/models/schemas.py``. Keep these in sync when the API contract
 * changes.
 */

export type BracketLevel = 1 | 2 | 3 | 4 | 5;

export type DeckSource = 'llm' | 'heuristic';

export interface DeckResponse {
  version: number;
  commander: MagicCard;
  bracket: BracketLevel;
  bracket_description: string;
  prompt: string;
  decklist: MagicCard[];
  explanation: string;
  notes: string[];
  source: DeckSource;
}

export interface BuildDeckPayload {
  commander: string;
  bracket: BracketLevel;
  prompt: string;
  gamechangers: string[];
  banned_list: string[];
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

export interface AutocompleteResponse {
  suggestions: string[];
}

// types/mtg.ts

export interface ScryfallImageUris {
  small: string;
  normal: string;
  large: string;
  png: string;
  art_crop: string;
  border_crop: string;
}

export interface ScryfallCardFace {
  name: string;
  image_uris?: ScryfallImageUris;
  type_line: string;
  oracle_text?: string;
}

export interface MagicCard {
  id: string;
  name: string;
  count: number;
  category?: string;
  type_line: string;
  oracle_text?: string;
  mana_cost?: string;
  cmc: number;
  colors?: string[];
  color_identity: string[];
  // Top-level image_uris for single-faced cards
  image_uris?: ScryfallImageUris;
  // card_faces for double-faced cards (Transform/Meld/etc)
  card_faces?: ScryfallCardFace[];
  legalities: {
    commander: 'legal' | 'not_legal' | 'restricted' | 'banned';
  };
  keywords: string[];
}