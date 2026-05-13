import type {
  AutocompleteResponse,
  BuildDeckPayload,
  DeckResponse,
  HealthStatus,
  RevampDeckPayload,
} from './types';

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(detail.detail || `Request failed with status ${response.status}`);
  }
  return (await response.json()) as T;
}

export function buildDeck(payload: BuildDeckPayload): Promise<DeckResponse> {
  const { commander, bracket, prompt } = payload;
  return postJson<DeckResponse>('/api/decks/build', { commander, bracket, prompt });
}

export function revampDeck(payload: RevampDeckPayload): Promise<DeckResponse> {
  return postJson<DeckResponse>('/api/decks/revamp', payload);
}

export async function fetchHealth(): Promise<HealthStatus> {
  const response = await fetch('/api/health');
  if (!response.ok) throw new Error('Health check failed');
  return (await response.json()) as HealthStatus;
}

export interface AutocompleteOptions {
  signal?: AbortSignal;
  commanderOnly?: boolean;
}

export async function autocompleteCommanders(
  query: string,
  { signal, commanderOnly = true }: AutocompleteOptions = {},
): Promise<string[]> {
  const params = new URLSearchParams({ q: query, commander_only: String(commanderOnly) });
  const response = await fetch(`/api/cards/autocomplete?${params.toString()}`, { signal });
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(detail.detail || `Autocomplete failed with status ${response.status}`);
  }
  const body = (await response.json()) as AutocompleteResponse;
  return body.suggestions || [];
}
