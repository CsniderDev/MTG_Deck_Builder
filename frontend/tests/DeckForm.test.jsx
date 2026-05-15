import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DeckForm from '../src/components/DeckForm';

describe('DeckForm', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // CommanderAutocomplete debounces a fetch on every value change; stub it.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ suggestions: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });

  it('renders all three inputs and a submit button', () => {
    render(<DeckForm onSubmit={() => {}} loading={false} />);
    expect(screen.getByLabelText(/commander/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/bracket level/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/deck concept/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /build deck/i })).toBeInTheDocument();
  });

  it('lists all five bracket options', () => {
    render(<DeckForm onSubmit={() => {}} loading={false} />);
    const select = screen.getByLabelText(/bracket level/i);
    const options = Array.from(select.querySelectorAll('option'));
    expect(options).toHaveLength(5);
    expect(options.map((o) => o.value)).toEqual(['1', '2', '3', '4', '5']);
  });

  it('submits trimmed values with a numeric bracket', async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(<DeckForm onSubmit={onSubmit} loading={false} />);

    await user.type(screen.getByLabelText(/commander/i), '  Krenko, Mob Boss  ');
    await user.selectOptions(screen.getByLabelText(/bracket level/i), '4');
    await user.type(screen.getByLabelText(/deck concept/i), '  goblin tribal  ');
    await user.click(screen.getByRole('button', { name: /build deck/i }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({
      commander: 'Krenko, Mob Boss',
      bracket: 4,
      prompt: 'goblin tribal',
      gamechangers: [],
      banned_list: [],
    });
  });

  it('disables submit while loading and shows loading copy', () => {
    render(<DeckForm onSubmit={() => {}} loading={true} initial={{ commander: 'X', bracket: 2, prompt: '' }} />);
    const button = screen.getByRole('button', { name: /building deck/i });
    expect(button).toBeDisabled();
  });

  it('disables submit when commander is empty', () => {
    render(<DeckForm onSubmit={() => {}} loading={false} />);
    const button = screen.getByRole('button', { name: /build deck/i });
    expect(button).toBeDisabled();
  });

  it('honors initial values', () => {
    render(
      <DeckForm
        onSubmit={() => {}}
        loading={false}
        initial={{ commander: 'Atraxa', bracket: 5, prompt: 'cEDH stax' }}
      />,
    );
    expect(screen.getByLabelText(/commander/i)).toHaveValue('Atraxa');
    expect(screen.getByLabelText(/bracket level/i)).toHaveValue('5');
    expect(screen.getByLabelText(/deck concept/i)).toHaveValue('cEDH stax');
  });
});
