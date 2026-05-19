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
    substitutions: [],
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
        ['/api/decks/revamp', () => jsonResponse(makeDeckPayload(2, 'Atraxa, Praetors\' Voice'))],
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

  it('renders LLM substitution explanations after a revamp', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      routedFetch([
        ['/api/health', jsonResponse({ status: 'ok', llm_enabled: true })],
        ['/api/decks/build', () => jsonResponse(makeDeckPayload(1))],
        [
          '/api/decks/revamp',
          () =>
            jsonResponse({
              ...makeDeckPayload(2),
              substitutions: [
                {
                  removed: [{ name: 'Cultivate', count: 1 }],
                  added: [{ name: 'Nature\'s Lore', count: 1 }],
                  explanation: 'Nature\'s Lore ramps earlier and keeps the mana curve lower.',
                },
              ],
            }),
        ],
        [/\/api\/cards\/autocomplete/, jsonResponse({ suggestions: [] })],
      ]),
    );

    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/with gemini guidance/i);

    await user.type(screen.getByLabelText(/commander/i), 'Atraxa');
    await user.type(screen.getByLabelText(/deck concept/i), 'proliferate');
    await user.click(screen.getByRole('button', { name: /build deck/i }));
    await screen.findByText(/v1/);

    await user.type(screen.getByPlaceholderText(/swap out the infinite combos/i), 'more efficient ramp');
    await user.click(screen.getByRole('button', { name: /generate v2/i }));

    expect(await screen.findByText(/LLM substitutions/i)).toBeInTheDocument();
    expect(screen.getByText(/Cultivate/)).toBeInTheDocument();
    expect(screen.getByText(/^Nature's Lore$/)).toBeInTheDocument();
    expect(screen.getByText(/ramps earlier and keeps the mana curve lower/i)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalled();
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

  it('can start from an existing decklist and call the revamp endpoint directly', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(
      routedFetch([
        ['/api/health', jsonResponse({ status: 'ok', llm_enabled: true })],
        ['/api/decks/revamp', () => jsonResponse(makeDeckPayload(1))],
        [/\/api\/cards\/autocomplete/, jsonResponse({ suggestions: [] })],
      ]),
    );

    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/with gemini guidance/i);

    await user.click(screen.getByRole('tab', { name: /start from existing decklist/i }));
    await user.type(screen.getByLabelText(/commander/i), 'Atraxa');
    await user.type(screen.getByLabelText(/what should the ai do to this deck/i), 'power it down');
    await user.type(screen.getByLabelText(/existing decklist/i), '1 Atraxa, Praetors\' Voice{enter}1 Sol Ring');
    await user.click(screen.getByRole('button', { name: /revise decklist/i }));

    expect(await screen.findByText(/v1/)).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/decks/revamp',
      expect.objectContaining({ method: 'POST' }),
    );
    const body = JSON.parse(fetchSpy.mock.calls.find((call) => call[0] === '/api/decks/revamp')[1].body);
    expect(body.commander).toBe('Atraxa');
    expect(body.previous_version).toBe(0);
    expect(body.change_request).toBe('power it down');
    expect(body.previous_decklist).toEqual([{ name: 'Sol Ring', count: 1 }]);
  });
});
