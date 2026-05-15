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
  const { commander, bracket, prompt, gamechangers, banned_list } = payload;
  return postJson<DeckResponse>('/api/decks/build', { commander, bracket, prompt, gamechangers, banned_list });
}

export function revampDeck(payload: RevampDeckPayload): Promise<DeckResponse> {
  return postJson<DeckResponse>('/api/decks/revamp', payload);
}

export async function fetchHealth(): Promise<HealthStatus> {
  const response = await fetch('/api/health');
  if (!response.ok) throw new Error('Health check failed');
  return (await response.json()) as HealthStatus;
}

export async function fetchBannedListAndGamechangers(): Promise<{ banned_list: string[]; gamechangers: string[] }> {
  const [bannedList, gamechangers] = await Promise.all([
    fetchBannedList(),
    fetchGamechangers()
  ]);
  return { banned_list: bannedList, gamechangers };
}

export async function fetchBannedList(): Promise<string[]> {
  const response = await fetch('/api/banned');
  if (!response.ok) throw new Error('Failed to fetch banned list');
  const data = (await response.json()) as { banned_list: string[] };
  console.log('Fetched banned list:', data);
  return data.banned_list || [];
}

export async function fetchGamechangers(): Promise<string[]> {
  const response = await fetch('/api/gamechangers');
  if (!response.ok) throw new Error('Failed to fetch gamechangers');
  const data = (await response.json()) as { gamechangers: string[] };
  console.log('Fetched gamechangers:', data);
  return data.gamechangers || [];
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
