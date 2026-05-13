import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../src/App';

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

function makeDeckPayload(version = 1, commanderName = 'Atraxa, Praetors\' Voice') {
  return {
    version,
    commander: { name: commanderName, count: 1, category: 'Commander' },
    bracket: 3,
    bracket_description: 'Upgraded.',
    prompt: 'proliferate',
    decklist: [{ name: 'Sol Ring', count: 1, category: 'Ramp' }],
    explanation: 'Plan.',
    notes: [],
    source: 'llm',
  };
}

function urlOf(input) {
  return typeof input === 'string' ? input : input?.url ?? '';
}

function routedFetch(routes) {
  return vi.fn((input) => {
    const url = urlOf(input);
    for (const [pattern, response] of routes) {
      if (typeof pattern === 'string' ? url === pattern : pattern.test(url)) {
        return Promise.resolve(typeof response === 'function' ? response() : response);
      }
    }
    return Promise.resolve(jsonResponse({}));
  });
}

describe('App', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('shows a warning when LLM is disabled', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      routedFetch([
        ['/api/health', jsonResponse({ status: 'ok', llm_enabled: false })],
      ]),
    );
    render(<App />);
    expect(await screen.findByText(/no gemini api key configured/i)).toBeInTheDocument();
  });

  it('hides the LLM warning when Gemini is configured', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      routedFetch([
        ['/api/health', jsonResponse({ status: 'ok', llm_enabled: true })],
      ]),
    );
    render(<App />);
    await waitFor(() => {
      expect(screen.queryByText(/no gemini api key configured/i)).not.toBeInTheDocument();
    });
    expect(screen.getByText(/with gemini guidance/i)).toBeInTheDocument();
  });

  it('builds a deck and then revamps it, incrementing the version', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      routedFetch([
        ['/api/health', jsonResponse({ status: 'ok', llm_enabled: true })],
        ['/api/decks/build', () => jsonResponse(makeDeckPayload(1))],
        ['/api/decks/revamp', () => jsonResponse(makeDeckPayload(2))],
        [/\/api\/cards\/autocomplete/, jsonResponse({ suggestions: [] })],
      ]),
    );

    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/with gemini guidance/i);

    await user.type(screen.getByLabelText(/commander/i), 'Atraxa');
    await user.type(screen.getByLabelText(/deck concept/i), 'proliferate');
    await user.click(screen.getByRole('button', { name: /build deck/i }));

    expect(await screen.findByText(/v1/)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/decks/build',
      expect.objectContaining({ method: 'POST' }),
    );

    await user.type(screen.getByPlaceholderText(/swap out the infinite combos/i), 'fewer combos');
    await user.click(screen.getByRole('button', { name: /generate v2/i }));

    expect(await screen.findByText(/v2/)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenLastCalledWith(
      '/api/decks/revamp',
      expect.objectContaining({ method: 'POST' }),
    );
    const revampBody = JSON.parse(fetchSpy.mock.lastCall[1].body);
    expect(revampBody.commander).toBe('Atraxa');
    expect(revampBody.previous_version).toBe(1);
    expect(revampBody.change_request).toBe('fewer combos');
  });

  it('surfaces error messages from the API', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      routedFetch([
        ['/api/health', jsonResponse({ status: 'ok', llm_enabled: true })],
        [
          '/api/decks/build',
          () =>
            new Response(JSON.stringify({ detail: "'Foo' is not a valid commander." }), {
              status: 400,
              headers: { 'Content-Type': 'application/json' },
            }),
        ],
        [/\/api\/cards\/autocomplete/, jsonResponse({ suggestions: [] })],
      ]),
    );

    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/with gemini guidance/i);

    await user.type(screen.getByLabelText(/commander/i), 'Foo');
    await user.click(screen.getByRole('button', { name: /build deck/i }));

    expect(await screen.findByText(/'Foo' is not a valid commander\./)).toBeInTheDocument();
  });
});
