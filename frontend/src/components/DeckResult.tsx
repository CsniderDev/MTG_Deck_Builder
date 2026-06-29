import React, { useMemo, useState } from 'react';
import type { DeckResponse, MagicCard } from '../types';
import MagicCardItem from './MagicCard';
import DeckStats from './DeckStats';
import DeckDiff from './DeckDiff';

function buildDeckText(deck: DeckResponse): string {
  /** Convert a rendered deck into a text decklist suitable for clipboard export. */
  const lines: string[] = [`1 ${deck.commander.name}`];
  if (deck.secondary_commander) {
    lines.push(`1 ${deck.secondary_commander.name}`);
  }
  const grouped = new Map<string, MagicCard[]>();
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
  onRevamp: (changeRequest: string) => Promise<boolean>;
  revamping?: boolean;
}

export default function DeckResult({
  deck,
  onRevamp,
  revamping,
}: DeckResultProps): React.ReactElement {
  /** Render the current deck, explanation, and revamp controls. */
  const [changeRequest, setChangeRequest] = useState<string>('');
  const [copied, setCopied] = useState<boolean>(false);

  const decklistText = useMemo(() => buildDeckText(deck), [deck]);
  const commandZoneSize = deck.secondary_commander ? 2 : 1;
  const totalCards =
    deck.decklist.reduce((sum, c) => sum + (c.count || 1), 0) + commandZoneSize;

  async function handleCopy(): Promise<void> {
    /** Copy the current decklist text to the clipboard and flash success state. */
    try {
      await navigator.clipboard.writeText(decklistText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  async function handleRevampSubmit(event: React.FormEvent<HTMLFormElement>): Promise<void> {
    /** Submit the inline revamp request and clear the text field only after success. */
    event.preventDefault();
    if (!changeRequest.trim()) return;
    const succeeded = await onRevamp(changeRequest.trim());
    if (succeeded) {
      setChangeRequest('');
    }
  }

  const grouped = new Map<string, MagicCard[]>();
  for (const card of deck.decklist) {
    const cat = card.category || 'Other';
    if (!grouped.has(cat)) grouped.set(cat, []);
    grouped.get(cat)!.push(card);
  }

  return (
    <section className="deck-result">
      <header className="deck-result__header">
        <div className="commander-header">
          <h2>
            {deck.secondary_commander ? `${deck.commander.name} + ${deck.secondary_commander.name}` : deck.commander.name}{' '}
            <span className="badge">v{deck.version}</span>
          </h2>

          <p className="muted">
            Bracket {deck.bracket} - {deck.bracket_description}
          </p>
          <p className="muted">
            {totalCards} cards total - source: {deck.source}
          </p>

          <div className="commander-showcase">
            <div className="commander-image">
              <MagicCardItem
                card={deck.commander as MagicCard}
                disableHover={true}
              />
            </div>
            {deck.secondary_commander && (
              <div className="commander-image commander-image--secondary">
                <MagicCardItem
                  card={deck.secondary_commander as MagicCard}
                  disableHover={true}
                />
              </div>
            )}
          </div>
        </div>
      </header>

      <DeckStats deck={deck} />
      {deck?.substitutions?.length ? <DeckDiff substitutions={deck.substitutions} /> : null}
      <div className="deck-result__body">
        <div className="deck-list">
          {[...grouped.entries()].map(([cat, cards]) => (
            <div className="category" key={cat}>
              <h3>{cat}</h3>
              <ul className="card-stack-list">
                {cards.map((card) => (
                  <li key={card.name} className="card-stack-item">
                    <div className="card-stack-media">
                      <MagicCardItem card={card as MagicCard} />
                    </div>
                    <div className="card-label">
                      <span className="count">{card.count}x</span> {card.name}
                    </div>
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
            <input             
              type="text"
              value={changeRequest}
              onChange={(e) => setChangeRequest(e.target.value)}
              placeholder="e.g. 'Swap out the infinite combos, add more board wipes, lower the curve.'"
            />
            <button type="submit" className="secondary" disabled={revamping || !changeRequest.trim()}>
              {revamping ? 'Revamping…' : `Generate v${deck.version + 1}`}
            </button>
            <button type="button" className="primary" onClick={handleCopy}>
              {copied ? 'Copied!' : 'Copy decklist'}
            </button>
          </form>
        </aside>
      </div>
    </section>
  );
}
