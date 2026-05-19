import React, { useEffect, useState } from 'react';
import CommanderAutocomplete from './CommanderAutocomplete';
import { fetchCommanderCompanionOptions } from '../api';
import type { BracketLevel, BuildDeckPayload, DeckFormMode, ExistingDeckActionPayload } from '../types';

interface BracketOption {
  value: BracketLevel;
  label: string;
}

const BRACKETS: ReadonlyArray<BracketOption> = [
  { value: 1, label: '1 - Exhibition (ultra-casual)' },
  { value: 2, label: '2 - Core (precon level)' },
  { value: 3, label: '3 - Upgraded' },
  { value: 4, label: '4 - Optimized (high power)' },
  { value: 5, label: '5 - cEDH' },
];

export interface DeckFormProps {
  mode?: DeckFormMode;
  onSubmit: (values: BuildDeckPayload | ExistingDeckActionPayload) => void;
  loading?: boolean;
  initial?: Partial<ExistingDeckActionPayload> | null;
}

export default function DeckForm({ mode = 'build', onSubmit, loading, initial }: DeckFormProps): React.ReactElement {
  /** Render the shared deck form for both new builds and imported-deck revisions. */
  const isExistingMode = mode === 'existing';
  const [commander, setCommander] = useState<string>(initial?.commander ?? '');
  const [bracket, setBracket] = useState<BracketLevel>(initial?.bracket ?? 2);
  const [prompt, setPrompt] = useState<string>(initial?.prompt ?? '');
  const [decklistText, setDecklistText] = useState<string>(initial?.decklist_text ?? '');
  const [secondaryCommander, setSecondaryCommander] = useState<string>(initial?.secondary_commander ?? '');
  const [secondaryLabel, setSecondaryLabel] = useState<string>('');
  const [secondaryOptions, setSecondaryOptions] = useState<string[]>([]);

  useEffect(() => {
    setCommander(initial?.commander ?? '');
    setBracket(initial?.bracket ?? 2);
    setPrompt(initial?.prompt ?? '');
    setDecklistText(initial?.decklist_text ?? '');
    setSecondaryCommander(initial?.secondary_commander ?? '');
  }, [initial, mode]);

  useEffect(() => {
    const trimmedCommander = commander.trim();
    if (trimmedCommander.length < 2) {
      setSecondaryCommander('');
      setSecondaryLabel('');
      setSecondaryOptions([]);
      return undefined;
    }

    const controller = new AbortController();
    fetchCommanderCompanionOptions(trimmedCommander, { signal: controller.signal })
      .then((payload) => {
        if (controller.signal.aborted) return;
        const options = payload.options || [];
        setSecondaryLabel(payload.relationship || 'Secondary commander');
        setSecondaryOptions(options);
        setSecondaryCommander((current) => (current && options.includes(current) ? current : ''));
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setSecondaryCommander('');
        setSecondaryLabel('');
        setSecondaryOptions([]);
      });

    return () => controller.abort();
  }, [commander]);


  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    /** Package the form values into the correct payload for the active workflow. */
    event.preventDefault();
    if (!commander.trim()) return;
    if (isExistingMode && (!prompt.trim() || !decklistText.trim())) return;
    const gamechangers = localStorage.getItem('mtg_gamechangers');
    const bannedList = localStorage.getItem('mtg_banned_list');
    const gamechangersState = gamechangers ? JSON.parse(gamechangers) : [];
    const bannedListState = bannedList ? JSON.parse(bannedList) : [];
    const commonValues = {
      commander: commander.trim(),
      secondary_commander: secondaryCommander.trim() || undefined,
      bracket,
      prompt: prompt.trim(),
      gamechangers: gamechangersState,
      banned_list: bannedListState,
    };
    if (isExistingMode) {
      onSubmit({ ...commonValues, decklist_text: decklistText.trim() });
      return;
    }
    onSubmit(commonValues);
  }

  return (
    <form className="deck-form" onSubmit={handleSubmit}>
      <label className="field">
        <span>Commander</span>
        <CommanderAutocomplete
          value={commander}
          onChange={setCommander}
          placeholder="e.g. Atraxa, Praetors' Voice"
          required
        />
      </label>

      <label className="field">
        <span>Bracket level</span>
        <select
          value={bracket}
          onChange={(e) => setBracket(Number(e.target.value) as BracketLevel)}
        >
          {BRACKETS.map((b) => (
            <option key={b.value} value={b.value}>
              {b.label}
            </option>
          ))}
        </select>
      </label>

      {secondaryOptions.length > 0 && (
        <label className="field">
          <span>{secondaryLabel || 'Secondary commander / background'}</span>
          <CommanderAutocomplete
            value={secondaryCommander}
            onChange={setSecondaryCommander}
            placeholder={`Select a valid ${secondaryLabel.toLowerCase() || 'option'}`}
            localSuggestions={secondaryOptions}
            commanderOnly={false}
          />
        </label>
      )}

      <label className="field">
        <span>{isExistingMode ? 'What should the AI do to this deck?' : 'Deck concept / prompt'}</span>
        <textarea
          rows={isExistingMode ? 4 : 5}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder={
            isExistingMode
              ? "e.g. 'Power this deck down to bracket 2, cut fast mana, and improve consistency on a budget.'"
              : "Describe the deck you want. e.g. 'Superfriends with proliferate, lean into +1/+1 counters, avoid infinite combos, budget under $300.'"
          }
        />
      </label>

      {isExistingMode && (
        <label className="field">
          <span>Existing decklist</span>
          <textarea
            rows={10}
            value={decklistText}
            onChange={(e) => setDecklistText(e.target.value)}
            placeholder={"Paste one card per line, e.g.\n1 Atraxa, Praetors' Voice\n1 Sol Ring\n1 Arcane Signet\n8 Forest"}
          />
        </label>
      )}

      <button
        type="submit"
        className="primary"
        disabled={loading || !commander.trim() || (isExistingMode && (!prompt.trim() || !decklistText.trim()))}
      >
        {loading ? (isExistingMode ? 'Updating deck…' : 'Building deck…') : (isExistingMode ? 'Revise decklist' : 'Build deck')}
      </button>
    </form>
  );
}
