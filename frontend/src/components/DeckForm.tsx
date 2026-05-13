import React, { useState } from 'react';
import CommanderAutocomplete from './CommanderAutocomplete';
import type { BracketLevel, BuildDeckPayload } from '../types';

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
  onSubmit: (values: BuildDeckPayload) => void;
  loading?: boolean;
  initial?: BuildDeckPayload | null;
}

export default function DeckForm({ onSubmit, loading, initial }: DeckFormProps): React.ReactElement {
  const [commander, setCommander] = useState<string>(initial?.commander ?? '');
  const [bracket, setBracket] = useState<BracketLevel>(initial?.bracket ?? 2);
  const [prompt, setPrompt] = useState<string>(initial?.prompt ?? '');

  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!commander.trim()) return;
    onSubmit({ commander: commander.trim(), bracket, prompt: prompt.trim() });
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

      <label className="field">
        <span>Deck concept / prompt</span>
        <textarea
          rows={5}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Describe the deck you want. e.g. 'Superfriends with proliferate, lean into +1/+1 counters, avoid infinite combos, budget under $300.'"
        />
      </label>

      <button type="submit" className="primary" disabled={loading || !commander.trim()}>
        {loading ? 'Building deck…' : 'Build deck'}
      </button>
    </form>
  );
}
