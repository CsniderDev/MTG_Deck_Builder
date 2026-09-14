import React, { useEffect, useState } from 'react';
import DeckForm from './components/DeckForm';
import DeckResult from './components/DeckResult';
import { buildDeck, fetchHealth, revampDeck, fetchBannedListAndGamechangers } from './api';
import type {
  BuildDeckPayload,
  DeckFormMode,
  DeckSubstitution,
  DeckResponse,
  ExistingDeckActionPayload,
  HealthStatus,
  MagicCard,
} from './types';

const TRAILING_SET_CODE_RE = /\s*(?:\([A-Za-z0-9]{2,6}\)|\[[A-Za-z0-9]{2,6}\])(?=\s*[0-9]+[A-Za-z]?(?:\s*[*★☆])*\s*$)\s*/;
const TRAILING_COLLECTOR_NUMBER_RE = /\s+[0-9]+[A-Za-z]?(?:\s*[*★☆])*\s*$/;

type DeckSession =
  | { mode: 'build'; values: BuildDeckPayload }
  | { mode: 'existing'; values: ExistingDeckActionPayload };

function deckIdentitySignature(deck: DeckResponse): string {
  /** Build a stable signature for the actual deck contents, ignoring version/notes/explanation metadata. */
  const library = [...deck.decklist]
    .map((card) => ({ name: card.name.trim().toLowerCase(), count: card.count || 1 }))
    .sort((a, b) => a.name.localeCompare(b.name) || a.count - b.count);
  return JSON.stringify({
    commander: deck.commander.name.trim().toLowerCase(),
    secondary_commander: deck.secondary_commander?.name?.trim().toLowerCase() || '',
    decklist: library,
  });
}

function normalizedCardKey(name: string): string {
  /** Build a stable key for matching deck cards by name regardless of case/spacing. */
  return name.trim().toLowerCase();
}

function cloneMagicCard(card: MagicCard, countOverride?: number): MagicCard {
  /** Clone a card row so local deck edits do not mutate shared response objects. */
  return {
    ...card,
    colors: card.colors ? [...card.colors] : undefined,
    color_identity: card.color_identity ? [...card.color_identity] : undefined,
    produced_mana: card.produced_mana ? [...card.produced_mana] : undefined,
    image_uris: card.image_uris ? { ...card.image_uris } : undefined,
    card_faces: card.card_faces?.map((face) => ({
      ...face,
      image_uris: face.image_uris ? { ...face.image_uris } : undefined,
    })),
    count: countOverride ?? card.count,
  };
}

function toCompactDecklistPayload(cards: MagicCard[]): MagicCard[] {
  /** Reduce deck cards to the minimal payload needed for revamp requests. */
  return cards.map((card) => ({
    name: card.name,
    count: card.count,
    category: card.category,
  }));
}

function applySubstitutionRevert(
  deck: DeckResponse,
  substitution: DeckSubstitution,
  previousLibrary: MagicCard[] | null,
): DeckResponse {
  /** Locally undo one substitution by removing added cards and restoring removed cards. */
  const cardsByKey = new Map<string, MagicCard>(
    deck.decklist.map((card) => [normalizedCardKey(card.name), cloneMagicCard(card)]),
  );
  const previousCardsByKey = new Map<string, MagicCard>(
    (previousLibrary ?? []).map((card) => [normalizedCardKey(card.name), card]),
  );

  for (const card of substitution.added) {
    const key = normalizedCardKey(card.name);
    const existing = cardsByKey.get(key);
    if (!existing) continue;
    const nextCount = (existing.count || 1) - (card.count || 1);
    if (nextCount > 0) {
      cardsByKey.set(key, cloneMagicCard(existing, nextCount));
    } else {
      cardsByKey.delete(key);
    }
  }

  for (const card of substitution.removed) {
    const key = normalizedCardKey(card.name);
    const existing = cardsByKey.get(key);
    const previousCard = previousCardsByKey.get(key);
    if (existing) {
      cardsByKey.set(key, cloneMagicCard(existing, (existing.count || 1) + (card.count || 1)));
      continue;
    }
    const baseCard = previousCard ?? card;
    cardsByKey.set(key, cloneMagicCard(baseCard, card.count || 1));
  }

  return {
    ...deck,
    decklist: [...cardsByKey.values()],
    substitutions: deck.substitutions.filter((candidate) => candidate !== substitution),
  };
}

function scrubDecklistCardName(rawName: string): string {
  /** Remove trailing set-code / collector-number decorations from pasted decklist entries. */
  return rawName
    .replace(TRAILING_SET_CODE_RE, ' ')
    .replace(TRAILING_COLLECTOR_NUMBER_RE, '')
    .trim();
}

function parseDecklistText(decklistText: string, commanderNames: string[]): MagicCard[] {
  /** Parse a pasted text decklist into API-ready card rows while removing the commander line. */
  const commanderKeys = commanderNames
    .map((name) => scrubDecklistCardName(name).trim().toLowerCase())
    .filter(Boolean);
  const cards: MagicCard[] = [];
  for (const rawLine of decklistText.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('//') || line.startsWith('#')) continue;
    const match = line.match(/^([0-9]+)x?\s+(.+)$/i);
    if (!match) {
      throw new Error(`Could not parse decklist line: ${line}`);
    }
    const count = Number(match[1]);
    const name = scrubDecklistCardName(match[2].trim());
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
  const [previousLibraryForCurrentDeck, setPreviousLibraryForCurrentDeck] = useState<MagicCard[] | null>(null);
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
      setPreviousLibraryForCurrentDeck(null);
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
      setPreviousLibraryForCurrentDeck(previousDecklist.map((card) => cloneMagicCard(card)));
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

  async function handleRevamp(changeRequest: string): Promise<boolean> {
    /** Revamp the currently displayed deck into the next numbered version. */
    if (!newDeck || !session) return false;
    setRevamping(true);
    setError('');
    try {
      const previousDeckSignature = deckIdentitySignature(newDeck);
      const previousLibrary = newDeck.decklist.map((card) => cloneMagicCard(card));
      const basePayload = {
        commander: session.values.commander,
        secondary_commander: session.values.secondary_commander,
        bracket: session.values.bracket,
        prompt: session.values.prompt,
        gamechangers,
        banned_list: bannedList,
        previous_version: newDeck.version,
        previous_decklist: toCompactDecklistPayload(newDeck.decklist),
        change_request: changeRequest,
      };
      const result = await revampDeck(basePayload);
      setNewDeck(result);
      setPreviousLibraryForCurrentDeck(previousLibrary);
      return deckIdentitySignature(result) !== previousDeckSignature;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return false;
    } finally {
      setRevamping(false);
    }
  }

  function handleRevertSubstitution(substitution: DeckSubstitution): void {
    /** Locally undo one displayed substitution so the user can reject a specific swap. */
    setNewDeck((currentDeck) => {
      if (!currentDeck) return currentDeck;
      return applySubstitutionRevert(currentDeck, substitution, previousLibraryForCurrentDeck);
    });
  }

  return (
    <div className="app">
      <header className="app__header">
        <h1>MTG Commander Deck Forge</h1>
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
        {newDeck && (
          <DeckResult
            deck={newDeck}
            onRevamp={handleRevamp}
            onRevertSubstitution={handleRevertSubstitution}
            revamping={revamping}
          />
        )}
      </main>
    </div>
  );
}
