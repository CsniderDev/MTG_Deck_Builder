import React, { useEffect, useId, useRef, useState } from 'react';
import { autocompleteCommanders } from '../api';

const MIN_QUERY = 2;
const DEBOUNCE_MS = 250;

export interface CommanderAutocompleteProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  required?: boolean;
  disabled?: boolean;
}

export default function CommanderAutocomplete({
  value,
  onChange,
  placeholder,
  required,
  disabled,
}: CommanderAutocompleteProps): React.ReactElement {
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [open, setOpen] = useState<boolean>(false);
  const [activeIndex, setActiveIndex] = useState<number>(-1);
  const [loading, setLoading] = useState<boolean>(false);
  const listId = useId();
  const abortRef = useRef<AbortController | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const skipNextFetchRef = useRef<boolean>(false);

  useEffect(() => {
    if (skipNextFetchRef.current) {
      skipNextFetchRef.current = false;
      return undefined;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const trimmed = (value || '').trim();
    if (trimmed.length < MIN_QUERY) {
      setSuggestions([]);
      setOpen(false);
      setActiveIndex(-1);
      return undefined;
    }
    debounceRef.current = setTimeout(async () => {
      if (abortRef.current) abortRef.current.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      setLoading(true);
      try {
        const names = await autocompleteCommanders(trimmed, { signal: controller.signal });
        setSuggestions(names);
        setOpen(names.length > 0);
        setActiveIndex(-1);
      } catch (err) {
        if (!(err instanceof DOMException) || err.name !== 'AbortError') {
          setSuggestions([]);
          setOpen(false);
        }
      } finally {
        setLoading(false);
      }
    }, DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [value]);

  function pick(name: string): void {
    skipNextFetchRef.current = true;
    onChange(name);
    setSuggestions([]);
    setOpen(false);
    setActiveIndex(-1);
  }

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
