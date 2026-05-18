import React, { useEffect, useState } from 'react';
import DeckForm from './components/DeckForm';
import DeckResult from './components/DeckResult';
import { buildDeck, fetchHealth, revampDeck, fetchBannedListAndGamechangers } from './api';
import type { BuildDeckPayload, DeckResponse, HealthStatus } from './types';

export default function App(): React.ReactElement {
  const [deck, setDeck] = useState<DeckResponse | null>(null);
  const [formState, setFormState] = useState<BuildDeckPayload | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [revamping, setRevamping] = useState<boolean>(false);
  const [error, setError] = useState<string>('');
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [bannedList, setBannedList] = useState<string[]>([]);
  const [gamechangers, setGamechangers] = useState<string[]>([]);

  useEffect(() => {

    if (!bannedList?.length || !gamechangers?.length) {
      fetchBannedListAndGamechangers()
        .then((data) => {
          setBannedList(data.banned_list || []);
          setGamechangers(data.gamechangers || []);
          localStorage.setItem('mtg_banned_list', JSON.stringify(data.banned_list || []));
          localStorage.setItem('mtg_gamechangers', JSON.stringify(data.gamechangers || []));
        })
        .catch(() => {
          setBannedList([]);
          setGamechangers([]);
        });
    } else {
      setBannedList(bannedList);
      setGamechangers(gamechangers);
    }

    console.log('gamechangers:', gamechangers);
    console.log('banned list:', bannedList);

    fetchHealth()
      .then(setHealth)
      .catch(() => setHealth({ status: 'unreachable', llm_enabled: false }));
  }, []);

  async function handleBuild(values: BuildDeckPayload): Promise<void> {
    setLoading(true);
    setError('');
    setFormState(values);
    try {
      const result = await buildDeck(values);
      setDeck(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  async function handleRevamp(changeRequest: string): Promise<void> {
    if (!deck || !formState) return;
    setRevamping(true);
    setError('');
    try {
      const result = await revampDeck({
        commander: formState.commander,
        bracket: formState.bracket,
        prompt: formState.prompt,
        gamechangers: gamechangers,
        banned_list: bannedList,
        previous_version: deck.version,
        previous_decklist: deck.decklist,
        change_request: changeRequest,
      });
      setDeck(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRevamping(false);
    }
  }

  return (
    <div className="app">
      <header className="app__header">
        <h1>MTG Commander Deck Builder</h1>
        <p className="muted">
          Powered by Scryfall and EDHREC{health?.llm_enabled ? ', with Gemini guidance' : ''}.
        </p>
        {health && !health.llm_enabled && (
          <p className="warning">
            No Gemini API key configured - decks will be built heuristically. Set GEMINI_API_KEY
            in backend/.env for LLM-guided builds.
          </p>
        )}
      </header>

      <main className="app__main">
        <DeckForm onSubmit={handleBuild} loading={loading} initial={formState}  />
        {error && <div className="error">{error}</div>}
        {deck && <DeckResult deck={deck} onRevamp={handleRevamp} revamping={revamping} />}
      </main>
    </div>
  );
}
