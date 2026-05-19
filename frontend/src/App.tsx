import React, { useEffect, useState } from 'react';
import DeckForm from './components/DeckForm';
import DeckResult from './components/DeckResult';
import DeckDiff from './components/DeckDiff';
import { buildDeck, fetchHealth, revampDeck, fetchBannedListAndGamechangers } from './api';
import type {
  BuildDeckPayload,
  DeckFormMode,
  DeckResponse,
  ExistingDeckActionPayload,
  HealthStatus,
  MagicCard,
} from './types';

type DeckSession =
  | { mode: 'build'; values: BuildDeckPayload }
  | { mode: 'existing'; values: ExistingDeckActionPayload };

function parseDecklistText(decklistText: string, commanderNames: string[]): MagicCard[] {
  /** Parse a pasted text decklist into API-ready card rows while removing the commander line. */
  const commanderKeys = commanderNames.map((name) => name.trim().toLowerCase()).filter(Boolean);
  const cards: MagicCard[] = [];
  for (const rawLine of decklistText.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('//') || line.startsWith('#')) continue;
    const match = line.match(/^([0-9]+)x?\s+(.+)$/i);
    if (!match) {
      throw new Error(`Could not parse decklist line: ${line}`);
    }
    const count = Number(match[1]);
    const name = match[2].trim();
    const normalizedName = name.toLowerCase();
    const isCommanderLine = commanderKeys.some((commanderKey) => (
      normalizedName === commanderKey ||
      normalizedName.includes(commanderKey) ||
      commanderKey.includes(normalizedName)
    ));
    if (!name || isCommanderLine) continue;
    cards.push({ name, count: Number.isFinite(count) && count > 0 ? count : 1 });
  }
  if (cards.length === 0) {
    throw new Error('No deck cards were found in the supplied decklist.');
  }
  return cards;
}

export default function App(): React.ReactElement {
  /** Render the top-level deck-builder UI and coordinate build/revamp workflows. */
  const [newDeck, setNewDeck] = useState<DeckResponse | null>(null);
  const [workflow, setWorkflow] = useState<DeckFormMode>('build');
  const [buildFormState, setBuildFormState] = useState<BuildDeckPayload | null>(null);
  const [existingFormState, setExistingFormState] = useState<ExistingDeckActionPayload | null>(null);
  const [session, setSession] = useState<DeckSession | null>(null);
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
    fetchHealth()
      .then(setHealth)
      .catch(() => setHealth({ status: 'unreachable', llm_enabled: false }));
  }, []);

  async function handleBuild(values: BuildDeckPayload): Promise<void> {
    /** Submit the standard build-from-commander workflow to the backend. */
    setLoading(true);
    setError('');
    setBuildFormState(values);
    try {
      const result = await buildDeck(values);
      setNewDeck(result);
      setSession({ mode: 'build', values });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  async function handleExistingDeckAction(values: ExistingDeckActionPayload): Promise<void> {
    /** Submit a pasted decklist plus requested changes through the revamp endpoint. */
    setLoading(true);
    setError('');
    setExistingFormState(values);
    try {
      const commanderNames = [values.commander, values.secondary_commander || ''];
      const previousDecklist = parseDecklistText(values.decklist_text, commanderNames);
      const result = await revampDeck({
        commander: values.commander,
        secondary_commander: values.secondary_commander,
        bracket: values.bracket,
        prompt: values.prompt,
        gamechangers: values.gamechangers,
        banned_list: values.banned_list,
        previous_version: 0,
        previous_decklist: previousDecklist,
        change_request: values.prompt,
      });
      setNewDeck(result);
      setSession({ mode: 'existing', values });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  async function handleFormSubmit(values: BuildDeckPayload | ExistingDeckActionPayload): Promise<void> {
    /** Route the shared form submission to the correct workflow handler. */
    if ('decklist_text' in values) {
      await handleExistingDeckAction(values);
      return;
    }
    await handleBuild(values);
  }

  async function handleRevamp(changeRequest: string): Promise<void> {
    /** Revamp the currently displayed deck into the next numbered version. */
    if (!newDeck || !session) return;
    setRevamping(true);
    setError('');
    try {
      const basePayload = {
        commander: session.values.commander,
        secondary_commander: session.values.secondary_commander,
        bracket: session.values.bracket,
        prompt: session.values.prompt,
        gamechangers,
        banned_list: bannedList,
        previous_version: newDeck.version,
        previous_decklist: newDeck.decklist,
        change_request: changeRequest,
      };
      const result = await revampDeck(basePayload);
      setNewDeck(result);
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
        <div className="workflow-switch" role="tablist" aria-label="Deck workflow">
          <button
            type="button"
            role="tab"
            aria-selected={workflow === 'build'}
            className={workflow === 'build' ? 'workflow-switch__button workflow-switch__button--active' : 'workflow-switch__button'}
            onClick={() => setWorkflow('build')}
          >
            Build from commander
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={workflow === 'existing'}
            className={workflow === 'existing' ? 'workflow-switch__button workflow-switch__button--active' : 'workflow-switch__button'}
            onClick={() => setWorkflow('existing')}
          >
            Start from existing decklist
          </button>
        </div>
        <DeckForm
          mode={workflow}
          onSubmit={handleFormSubmit}
          loading={loading}
          initial={workflow === 'build' ? buildFormState : existingFormState}
        />
        {error && <div className="error">{error}</div>}
        {newDeck && <DeckResult deck={newDeck} onRevamp={handleRevamp} revamping={revamping} />}
        {newDeck?.substitutions?.length ? <DeckDiff substitutions={newDeck.substitutions} /> : null}
      </main>
    </div>
  );
}
