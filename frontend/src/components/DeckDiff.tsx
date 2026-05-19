import React from 'react';
import type { DeckSubstitution, MagicCard } from '../types';

export interface DeckDiffProps {
  substitutions: DeckSubstitution[];
}

function formatSwapCards(cards: MagicCard[]): string {
  /** Format a group of added or removed cards into a short comma-separated label. */
  return cards
    .map((card) => (card.count > 1 ? `${card.count}x ${card.name}` : card.name))
    .join(', ');
}

export default function DeckDiff({ substitutions }: DeckDiffProps): React.ReactElement | null {
  /** Render the LLM's substitution-by-substitution explanation for a deck revision. */
  if (!substitutions.length) {
    return null;
  }

  return (
    <section className="deck-diff" aria-label="Deck substitutions">
      <h3>LLM substitutions</h3>
      <div className="deck-diff__list">
        {substitutions.map((substitution, index) => (
          <article key={`${formatSwapCards(substitution.removed)}-${formatSwapCards(substitution.added)}-${index}`} className="deck-diff__item">
            <div className="deck-diff__columns">
              <div>
                <p className="deck-diff__label">Removed</p>
                <p className="deck-diff__cards">{formatSwapCards(substitution.removed) || '—'}</p>
              </div>
              <div>
                <p className="deck-diff__label">Added</p>
                <p className="deck-diff__cards">{formatSwapCards(substitution.added) || '—'}</p>
              </div>
            </div>
            <p className="deck-diff__reason">{substitution.explanation}</p>
          </article>
        ))}
      </div>
    </section>
  );
}
