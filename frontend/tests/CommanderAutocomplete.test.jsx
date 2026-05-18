import { describe, it, expect, vi, beforeEach } from 'vitest';
import React, { useState } from 'react';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import CommanderAutocomplete from '../src/components/CommanderAutocomplete';

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function Wrapper({ initial = '', onPick }) {
  const [value, setValue] = useState(initial);
  return (
    <CommanderAutocomplete
      value={value}
      onChange={(v) => {
        setValue(v);
        onPick?.(v);
      }}
      placeholder="commander"
    />
  );
}

describe('CommanderAutocomplete', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('does not fetch suggestions for inputs shorter than 2 characters', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ suggestions: [] }),
    );
    const user = userEvent.setup();
    render(<Wrapper />);
    await user.type(screen.getByRole('combobox'), 'a');
    await new Promise((r) => setTimeout(r, 300));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('debounces and fetches suggestions, then displays them', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ suggestions: ["Atraxa, Praetors' Voice", 'Atraxa, Grand Unifier'] }),
    );
    const user = userEvent.setup();
    render(<Wrapper />);
    await user.type(screen.getByRole('combobox'), 'atra');

    const option = await screen.findByText("Atraxa, Praetors' Voice");
    expect(option).toBeInTheDocument();
    expect(screen.getByText('Atraxa, Grand Unifier')).toBeInTheDocument();
    expect(screen.getByRole('listbox')).toBeInTheDocument();
  });

  it('selects a suggestion by clicking it and closes the dropdown', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ suggestions: ["Atraxa, Praetors' Voice", 'Atraxa, Grand Unifier'] }),
    );
    const onPick = vi.fn();
    const user = userEvent.setup();
    render(<Wrapper onPick={onPick} />);
    await user.type(screen.getByRole('combobox'), 'atra');

    const option = await screen.findByText("Atraxa, Praetors' Voice");
    await user.click(option);

    expect(onPick).toHaveBeenLastCalledWith("Atraxa, Praetors' Voice");
    expect(screen.getByRole('combobox')).toHaveValue("Atraxa, Praetors' Voice");
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument());
  });

  it('navigates with arrow keys and selects with Enter', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ suggestions: ['Krenko, Mob Boss', 'Krenko, Tin Street Kingpin'] }),
    );
    const onPick = vi.fn();
    const user = userEvent.setup();
    render(<Wrapper onPick={onPick} />);
    await user.type(screen.getByRole('combobox'), 'krenko');

    await screen.findByText('Krenko, Mob Boss');
    const input = screen.getByRole('combobox');
    await user.type(input, '{ArrowDown}{ArrowDown}{Enter}');

    expect(onPick).toHaveBeenLastCalledWith('Krenko, Tin Street Kingpin');
    expect(input).toHaveValue('Krenko, Tin Street Kingpin');
  });

  it('closes the dropdown on Escape without changing the value', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ suggestions: ['Krenko, Mob Boss'] }),
    );
    const user = userEvent.setup();
    render(<Wrapper />);
    const input = screen.getByRole('combobox');
    await user.type(input, 'krenko');

    await screen.findByText('Krenko, Mob Boss');
    await user.type(input, '{Escape}');

    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument());
    expect(input).toHaveValue('krenko');
  });

  it('swallows fetch errors silently and shows no dropdown', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('{}', { status: 500, headers: { 'Content-Type': 'application/json' } }),
    );
    const user = userEvent.setup();
    render(<Wrapper />);
    await user.type(screen.getByRole('combobox'), 'atra');
    await act(async () => {
      await new Promise((r) => setTimeout(r, 350));
    });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  it('does not reopen stale suggestions after the input is cleared', async () => {
    let resolveFetch;
    vi.spyOn(globalThis, 'fetch').mockImplementation((_input, init) => new Promise((resolve, reject) => {
      resolveFetch = () => resolve(jsonResponse({ suggestions: ["Atraxa, Praetors' Voice"] }));
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));

    const user = userEvent.setup();
    render(<Wrapper />);
    const input = screen.getByRole('combobox');

    await user.type(input, 'atra');
    await act(async () => {
      await new Promise((r) => setTimeout(r, 300));
    });

    await user.clear(input);
    await act(async () => {
      resolveFetch?.();
      await Promise.resolve();
    });

    expect(input).toHaveValue('');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(screen.queryByText("Atraxa, Praetors' Voice")).not.toBeInTheDocument();
  });
});
