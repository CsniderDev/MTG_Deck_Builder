import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DeckResult from '../src/components/DeckResult';

function makeDeck(overrides = {}) {
  return {
    version: 1,
    commander: { name: 'Atraxa, Praetors\' Voice', count: 1, category: 'Commander' },
    bracket: 3,
    bracket_description: 'Upgraded: above-precon.',
    prompt: 'proliferate',
    decklist: [
      { name: 'Sol Ring', count: 1, category: 'Ramp' },
      { name: 'Arcane Signet', count: 1, category: 'Ramp' },
      { name: 'Swords to Plowshares', count: 1, category: 'Removal' },
      { name: 'Forest', count: 8, category: 'Land' },
    ],
    explanation: 'First paragraph.\n\nSecond paragraph.',
    notes: ['Padded 90 basic lands to reach 99.'],
    source: 'heuristic',
    ...overrides,
  };
}

describe('DeckResult', () => {
  it('renders commander, version badge, bracket, source and total count', () => {
    render(<DeckResult deck={makeDeck()} onRevamp={() => {}} revamping={false} />);
    expect(screen.getByRole('heading', { name: /Atraxa, Praetors' Voice/ })).toBeInTheDocument();
    expect(screen.getByText(/v1/)).toBeInTheDocument();
    expect(screen.getByText(/Bracket 3/)).toBeInTheDocument();
    expect(screen.getByText(/source: heuristic/)).toBeInTheDocument();
    // 1 + 1 + 1 + 8 (decklist) + 1 commander = 12
    expect(screen.getByText(/12 cards total/)).toBeInTheDocument();
  });

  it('groups cards by category', () => {
    render(<DeckResult deck={makeDeck()} onRevamp={() => {}} revamping={false} />);
    expect(screen.getByRole('heading', { name: /^Ramp$/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /^Removal$/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /^Land$/i })).toBeInTheDocument();
    expect(screen.getByText(/Sol Ring/)).toBeInTheDocument();
    expect(screen.getByText(/Arcane Signet/)).toBeInTheDocument();
  });

  it('renders each note', () => {
    render(<DeckResult deck={makeDeck()} onRevamp={() => {}} revamping={false} />);
    expect(screen.getByText(/Padded 90 basic lands/)).toBeInTheDocument();
  });

  it('renders multi-paragraph explanation', () => {
    render(<DeckResult deck={makeDeck()} onRevamp={() => {}} revamping={false} />);
    expect(screen.getByText('First paragraph.')).toBeInTheDocument();
    expect(screen.getByText('Second paragraph.')).toBeInTheDocument();
  });

  it('copies a formatted decklist to clipboard and flashes "Copied!"', async () => {
    // userEvent.setup() installs its own clipboard, so spy after it.
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });

    render(<DeckResult deck={makeDeck()} onRevamp={() => {}} revamping={false} />);
    await user.click(screen.getByRole('button', { name: /copy decklist/i }));

    expect(writeText).toHaveBeenCalledTimes(1);
    const text = writeText.mock.calls[0][0];
    expect(text).toContain("1 Atraxa, Praetors' Voice");
    expect(text).toContain('1 Sol Ring');
    expect(text).toContain('8 Forest');
    expect(await screen.findByRole('button', { name: /copied/i })).toBeInTheDocument();
  });

  it('submits the change request through onRevamp and clears the textarea', async () => {
    const onRevamp = vi.fn();
    const user = userEvent.setup();
    render(<DeckResult deck={makeDeck()} onRevamp={onRevamp} revamping={false} />);

    const textarea = screen.getByPlaceholderText(/swap out the infinite combos/i);
    await user.type(textarea, 'add two board wipes');
    await user.click(screen.getByRole('button', { name: /generate v2/i }));

    expect(onRevamp).toHaveBeenCalledWith('add two board wipes');
    expect(textarea).toHaveValue('');
  });

  it('disables revamp button while revamping', () => {
    render(<DeckResult deck={makeDeck({ version: 2 })} onRevamp={() => {}} revamping={true} />);
    const button = screen.getByRole('button', { name: /revamping/i });
    expect(button).toBeDisabled();
  });

  it('shows version badge for higher iterations', () => {
    render(<DeckResult deck={makeDeck({ version: 4 })} onRevamp={() => {}} revamping={false} />);
    expect(screen.getByText(/v4/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /generate v5/i })).toBeInTheDocument();
  });

  it('does not call onRevamp when change request is empty', async () => {
    const onRevamp = vi.fn();
    const user = userEvent.setup();
    render(<DeckResult deck={makeDeck()} onRevamp={onRevamp} revamping={false} />);
    const button = screen.getByRole('button', { name: /generate v2/i });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(onRevamp).not.toHaveBeenCalled();
  });
});
