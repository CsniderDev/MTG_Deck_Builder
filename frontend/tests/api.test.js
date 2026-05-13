import { describe, it, expect, vi, beforeEach } from 'vitest';
import { autocompleteCommanders, buildDeck, revampDeck, fetchHealth } from '../src/api';

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

describe('api helpers', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('buildDeck POSTs JSON to /api/decks/build and returns parsed body', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ version: 1, decklist: [] }),
    );
    const result = await buildDeck({ commander: 'Atraxa', bracket: 3, prompt: 'combo' });
    expect(result).toEqual({ version: 1, decklist: [] });

    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/decks/build',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const sent = JSON.parse(fetchSpy.mock.calls[0][1].body);
    expect(sent).toEqual({ commander: 'Atraxa', bracket: 3, prompt: 'combo' });
  });

  it('revampDeck POSTs JSON to /api/decks/revamp', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ version: 2 }),
    );
    const payload = {
      commander: 'Atraxa',
      bracket: 3,
      prompt: '',
      previous_version: 1,
      previous_decklist: [],
      change_request: 'less combo',
    };
    const result = await revampDeck(payload);
    expect(result).toEqual({ version: 2 });
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/decks/revamp',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('buildDeck throws using detail field on non-OK responses', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: "'X' is not a valid commander." }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await expect(
      buildDeck({ commander: 'X', bracket: 2, prompt: '' }),
    ).rejects.toThrow("'X' is not a valid commander.");
  });

  it('buildDeck throws with status message when detail is missing', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('{}', { status: 500, headers: { 'Content-Type': 'application/json' } }),
    );
    await expect(
      buildDeck({ commander: 'X', bracket: 2, prompt: '' }),
    ).rejects.toThrow(/status 500/);
  });

  it('fetchHealth returns parsed JSON when ok', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ status: 'ok', llm_enabled: true }),
    );
    const result = await fetchHealth();
    expect(result).toEqual({ status: 'ok', llm_enabled: true });
  });

  it('fetchHealth throws when response is not ok', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 503 }));
    await expect(fetchHealth()).rejects.toThrow('Health check failed');
  });

  it('autocompleteCommanders GETs /api/cards/autocomplete with query params and returns suggestions', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ suggestions: ["Atraxa, Praetors' Voice", 'Atraxa, Grand Unifier'] }),
    );
    const result = await autocompleteCommanders('atra');
    expect(result).toEqual(["Atraxa, Praetors' Voice", 'Atraxa, Grand Unifier']);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/cards/autocomplete?q=atra&commander_only=true');
    expect(init).toEqual(expect.objectContaining({ signal: undefined }));
  });

  it('autocompleteCommanders forwards an AbortSignal and commander_only flag', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ suggestions: [] }),
    );
    const controller = new AbortController();
    await autocompleteCommanders('bolt', { signal: controller.signal, commanderOnly: false });
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/cards/autocomplete?q=bolt&commander_only=false');
    expect(init.signal).toBe(controller.signal);
  });

  it('autocompleteCommanders throws with detail on non-OK responses', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'rate limited' }), {
        status: 429,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await expect(autocompleteCommanders('foo')).rejects.toThrow('rate limited');
  });
});
