import React from 'react';
import type { DeckSubstitution, MagicCard } from '../types';
import MagicCardItem from './MagicCard';

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
            <div className="deck-diff__swap">
              <div className="deck-diff__side">
                <p className="deck-diff__label">Removed</p>
                <div className="deck-diff__cards-grid">
                  {substitution.removed.length ? substitution.removed.map((card) => (
                    <div key={`removed-${card.name}`} className="deck-diff__card-pill deck-diff__card-pill--removed">
                      <div className="deck-diff__card-media">
                        <MagicCardItem card={card} disableHover={true} />
                      </div>
                      <span className="deck-diff__cards">{card.count > 1 ? `${card.count}x ` : ''}{card.name}</span>
                    </div>
                  )) : <p className="deck-diff__cards">—</p>}
                </div>
              </div>
              <div className="deck-diff__arrow" aria-hidden="true">→</div>
              <div className="deck-diff__side">
                <p className="deck-diff__label">Added</p>
                <div className="deck-diff__cards-grid">
                  {substitution.added.length ? substitution.added.map((card) => (
                    <div key={`added-${card.name}`} className="deck-diff__card-pill deck-diff__card-pill--added">
                      <div className="deck-diff__card-media">
                        <MagicCardItem card={card} disableHover={true} />
                      </div>
                      <span className="deck-diff__cards">{card.count > 1 ? `${card.count}x ` : ''}{card.name}</span>
                    </div>
                  )) : <p className="deck-diff__cards">—</p>}
                </div>
              </div>
            </div>
            <p className="deck-diff__reason">{substitution.explanation}</p>
          </article>
        ))}
      </div>
    </section>
  );
}
