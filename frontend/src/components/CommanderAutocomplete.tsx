import React, { useEffect, useId, useRef, useState } from 'react';
import { autocompleteCommanders } from '../api';
import { useDebounce } from './useDebounce'; // Adjust path as necessary

const MIN_QUERY = 2;
const DEBOUNCE_MS = 250;

export interface CommanderAutocompleteProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  required?: boolean;
  disabled?: boolean;
  localSuggestions?: string[];
  commanderOnly?: boolean;
}

export default function CommanderAutocomplete({
  value,
  onChange,
  placeholder,
  required,
  disabled,
  localSuggestions,
  commanderOnly = true,
}: CommanderAutocompleteProps): React.ReactElement {
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [open, setOpen] = useState<boolean>(false);
  const [activeIndex, setActiveIndex] = useState<number>(-1);
  const [loading, setLoading] = useState<boolean>(false);
  const listId = useId();
  
  const skipNextFetchRef = useRef<boolean>(false);

  // 1. Track the debounced string stream
  const debouncedSearchTerm = useDebounce(value, DEBOUNCE_MS);

  // 2. Localized useEffect hook
  useEffect(() => {
    const trimmed = (debouncedSearchTerm || '').trim();

    if (localSuggestions) {
      const filtered = localSuggestions.filter((name) =>
        !trimmed ? true : name.toLowerCase().includes(trimmed.toLowerCase()),
      );
      setSuggestions(filtered);
      setOpen(filtered.length > 0 && !disabled);
      setActiveIndex(-1);
      setLoading(false);
      return undefined;
    }

    if (skipNextFetchRef.current) {
      skipNextFetchRef.current = false;
      return undefined;
    }

    if (trimmed.length < MIN_QUERY || disabled) {
      setSuggestions([]);
      setOpen(false);
      setActiveIndex(-1);
      setLoading(false);
      return undefined;
    }

    const controller = new AbortController();
    setLoading(true);

    async function fireAPI() {
      try {
        const names = await autocompleteCommanders(trimmed, {
          signal: controller.signal,
          commanderOnly,
        });
        
        setSuggestions(names);
        setOpen(names.length > 0);
        setActiveIndex(-1);
      } catch (err) {
        if (!(err instanceof DOMException) || err.name !== 'AbortError') {
          setSuggestions([]);
          setOpen(false);
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    }

    fireAPI();

    return () => {
      controller.abort();
    };
  }, [debouncedSearchTerm, localSuggestions, commanderOnly, disabled]);

  // 3. Selection helper (Inside CommanderAutocomplete)
  function pick(name: string): void {
    skipNextFetchRef.current = true;
    setLoading(false);
    onChange(name);
    setSuggestions([]);
    setOpen(false);
    setActiveIndex(-1);
  }

  // 4. Keyboard Listener (Inside CommanderAutocomplete)
  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>): void {
    if (event.key === 'ArrowDown') {
      if (suggestions.length === 0) return;
      event.preventDefault();
      setOpen(true);
      setActiveIndex((idx) => (idx + 1) % suggestions.length);
    } else if (event.key === 'ArrowUp') {
      if (suggestions.length === 0) return;
      event.preventDefault();
      setOpen(true);
      setActiveIndex((idx) => (idx <= 0 ? suggestions.length - 1 : idx - 1));
    } else if (event.key === 'Enter') {
      if (open && activeIndex >= 0 && suggestions[activeIndex]) {
        event.preventDefault();
        pick(suggestions[activeIndex]);
      }
    } else if (event.key === 'Escape') {
      if (open) {
        event.preventDefault();
        setOpen(false);
        setActiveIndex(-1);
      }
    }
  }

  const showList = open && suggestions.length > 0;

  // 5. Explicit Component JSX Return
  return (
    <div className="autocomplete">
      <input
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        onFocus={() => suggestions.length > 0 && setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        placeholder={placeholder}
        autoComplete="off"
        required={required}
        disabled={disabled}
        role="combobox"
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={
          showList && activeIndex >= 0 ? `${listId}-opt-${activeIndex}` : undefined
        }
      />
      {loading && <span className="autocomplete__loading" aria-hidden="true">…</span>}
      {showList && (
        <ul id={listId} className="autocomplete__list" role="listbox">
          {suggestions.map((name, idx) => (
            <li
              key={name}
              id={`${listId}-opt-${idx}`}
              role="option"
              aria-selected={idx === activeIndex}
              className={
                idx === activeIndex ? 'autocomplete__item autocomplete__item--active' : 'autocomplete__item'
              }
              onMouseDown={(event) => {
                event.preventDefault();
                pick(name);
              }}
              onMouseEnter={() => setActiveIndex(idx)}
            >
              {name}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}