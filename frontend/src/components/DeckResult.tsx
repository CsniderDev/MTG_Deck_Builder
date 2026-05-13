import React, { useMemo, useState } from 'react';
import type { DeckCard, DeckResponse } from '../types';

function buildDeckText(deck: DeckResponse): string {
  const lines: string[] = [`1 ${deck.commander.name}`];
  const grouped = new Map<string, DeckCard[]>();
  for (const card of deck.decklist) {
    const cat = card.category || 'Other';
    if (!grouped.has(cat)) grouped.set(cat, []);
    grouped.get(cat)!.push(card);
  }
  for (const [cat, cards] of grouped) {
    for (const card of cards) lines.push(`${card.count} ${card.name}`);
  }
  return lines.join('\n');
}

export interface DeckResultProps {
  deck: DeckResponse;
  onRevamp: (changeRequest: string) => void;
  revamping?: boolean;
}

export default function DeckResult({
  deck,
  onRevamp,
  revamping,
}: DeckResultProps): React.ReactElement {
  const [changeRequest, setChangeRequest] = useState<string>('');
  const [copied, setCopied] = useState<boolean>(false);

  const decklistText = useMemo(() => buildDeckText(deck), [deck]);
  const totalCards =
    deck.decklist.reduce((sum, c) => sum + (c.count || 1), 0) + 1; // +1 commander

  async function handleCopy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(decklistText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  function handleRevampSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!changeRequest.trim()) return;
    onRevamp(changeRequest.trim());
    setChangeRequest('');
  }

  const grouped = new Map<string, DeckCard[]>();
  for (const card of deck.decklist) {
    const cat = card.category || 'Other';
    if (!grouped.has(cat)) grouped.set(cat, []);
    grouped.get(cat)!.push(card);
  }

  return (
    <section className="deck-result">
      <header className="deck-result__header">
        <div>
          <h2>
            {deck.commander.name} <span className="badge">v{deck.version}</span>
          </h2>
          <p className="muted">
            Bracket {deck.bracket} - {deck.bracket_description}
          </p>
          <p className="muted">
            {totalCards} cards total - source: {deck.source}
          </p>
        </div>
        <button type="button" className="primary" onClick={handleCopy}>
          {copied ? 'Copied!' : 'Copy decklist'}
        </button>
      </header>

      {deck.notes?.length > 0 && (
        <ul className="notes">
          {deck.notes.map((note, idx) => (
            <li key={idx}>{note}</li>
          ))}
        </ul>
      )}

      <div className="deck-result__body">
        <div className="deck-list">
          {[...grouped.entries()].map(([cat, cards]) => (
            <div className="category" key={cat}>
              <h3>{cat}</h3>
              <ul>
                {cards.map((card) => (
                  <li key={card.name}>
                    <span className="count">{card.count}</span> {card.name}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <aside className="deck-explanation">
          <h3>Explanation</h3>
          {deck.explanation.split(/\n+/).map((paragraph, idx) => (
            <p key={idx}>{paragraph}</p>
          ))}

          <h3>Revamp this deck</h3>
          <form onSubmit={handleRevampSubmit}>
            <textarea
              rows={4}
              value={changeRequest}
              onChange={(e) => setChangeRequest(e.target.value)}
              placeholder="e.g. 'Swap out the infinite combos, add more board wipes, lower the curve.'"
            />
            <button type="submit" className="secondary" disabled={revamping || !changeRequest.trim()}>
              {revamping ? 'Revamping…' : `Generate v${deck.version + 1}`}
            </button>
          </form>
        </aside>
      </div>
    </section>
  );
}
