import React, { useMemo, useState } from 'react';
import type { DeckResponse, DeckSubstitution, MagicCard } from '../types';
import MagicCardItem from './MagicCard';
import DeckStats from './DeckStats';
import DeckDiff from './DeckDiff';
import TurnstileWidget from './TurnstileWidget';

// Display groups, in on-screen order. A card with several types goes in the first
// matching group by TYPE_PRIORITY (e.g. an artifact creature is a Creature).
const TYPE_GROUPS = ['Creature', 'Planeswalker', 'Battle', 'Instant', 'Sorcery', 'Artifact', 'Enchantment', 'Land'];
const TYPE_PRIORITY = ['Land', 'Creature', 'Planeswalker', 'Battle', 'Artifact', 'Enchantment', 'Instant', 'Sorcery'];
const GROUP_LABELS: Record<string, string> = {
  Creature: 'Creatures',
  Planeswalker: 'Planeswalkers',
  Battle: 'Battles',
  Instant: 'Instants',
  Sorcery: 'Sorceries',
  Artifact: 'Artifacts',
  Enchantment: 'Enchantments',
  Land: 'Lands',
};

export function cardTypeGroup(card: MagicCard): string {
  /** Main card type of the front face; falls back to the backend category without a type line. */
  const typeLine = (card.type_line || card.card_faces?.[0]?.type_line || '').split('//')[0];
  if (!typeLine.trim()) return card.category || 'Other';
  // Drop subtypes ("Creature — Goblin") so a subtype never decides the group.
  const mainTypes = typeLine.split(/\s+[—-]\s+/)[0].toLowerCase().split(/\s+/);
  return TYPE_PRIORITY.find((type) => mainTypes.includes(type.toLowerCase())) || 'Other';
}

export function groupByType(cards: MagicCard[]): [string, MagicCard[]][] {
  /** Group cards by main type, ordered by TYPE_GROUPS, each group sorted by name. */
  const groups = new Map<string, MagicCard[]>();
  for (const card of cards) {
    const group = cardTypeGroup(card);
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group)!.push(card);
  }
  const rank = (group: string): number => {
    const idx = TYPE_GROUPS.indexOf(group);
    return idx === -1 ? TYPE_GROUPS.length : idx;
  };
  return [...groups.entries()]
    .sort(([a], [b]) => rank(a) - rank(b))
    .map(([group, list]) => [GROUP_LABELS[group] || group, [...list].sort((x, y) => x.name.localeCompare(y.name))]);
}

function buildDeckText(deck: DeckResponse): string {
  /** Convert a rendered deck into a text decklist suitable for clipboard export. */
  const lines: string[] = [`1 ${deck.commander.name}`];
  if (deck.secondary_commander) {
    lines.push(`1 ${deck.secondary_commander.name}`);
  }
  for (const [, cards] of groupByType(deck.decklist)) {
    for (const card of cards) lines.push(`${card.count} ${card.name}`);
  }
  return lines.join('\n');
}

export interface DeckResultProps {
  deck: DeckResponse;
  onRevamp: (changeRequest: string, turnstileToken?: string) => Promise<boolean>;
  onRevertSubstitution: (substitution: DeckSubstitution) => void;
  revamping?: boolean;
}

export default function DeckResult({
  deck,
  onRevamp,
  onRevertSubstitution,
  revamping,
}: DeckResultProps): React.ReactElement {
  /** Render the current deck, explanation, and revamp controls. */
  const [changeRequest, setChangeRequest] = useState<string>('');
  const [copied, setCopied] = useState<boolean>(false);
  const [turnstileToken, setTurnstileToken] = useState<string>('');

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
    // Only pass a second argument when a token actually exists, so callers/tests that
    // assert on onRevamp's call args aren't affected when Turnstile isn't configured.
    const succeeded = turnstileToken
      ? await onRevamp(changeRequest.trim(), turnstileToken)
      : await onRevamp(changeRequest.trim());
    if (succeeded) {
      setChangeRequest('');
    }
  }

  const grouped = useMemo(() => groupByType(deck.decklist), [deck.decklist]);

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
      {deck?.substitutions?.length ? (
        <DeckDiff substitutions={deck.substitutions} onRevertSubstitution={onRevertSubstitution} />
      ) : null}
      <div className="deck-result__body">
        <div className="deck-list">
          {grouped.map(([cat, cards]) => (
            <div className="category" key={cat}>
              <h3>
                {cat} <span className="category__count">({cards.reduce((sum, c) => sum + (c.count || 1), 0)})</span>
              </h3>
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
          {deck.plan?.strategy && (
            <p className="deck-plan">
              <strong>Game plan:</strong> {deck.plan.strategy}
            </p>
          )}
          <h3>Explanation</h3>
          {deck.explanation.split(/\n+/).map((paragraph, idx) => (
            <p key={idx}>{paragraph}</p>
          ))}

          <h3>Revamp this deck</h3>
          <form onSubmit={handleRevampSubmit}>
            <textarea
              className="revamp-request"
              rows={5}
              value={changeRequest}
              onChange={(e) => setChangeRequest(e.target.value)}
              onKeyDown={(e) => {
                // Enter adds a new line; Ctrl/Cmd+Enter submits.
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  e.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder="e.g. 'Swap out the infinite combos, add more board wipes, lower the curve.' (Ctrl+Enter to submit)"
            />
            <button type="submit" className="secondary" disabled={revamping || !changeRequest.trim()}>
              {revamping ? 'Revamping…' : `Generate v${deck.version + 1}`}
            </button>
            <button type="button" className="primary" onClick={handleCopy}>
              {copied ? 'Copied!' : 'Copy decklist'}
            </button>
            <TurnstileWidget onTokenChange={setTurnstileToken} />
          </form>
        </aside>
      </div>
    </section>
  );
}
