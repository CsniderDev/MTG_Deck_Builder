import React, { useState } from 'react';
import { getAccessCode, setAccessCode } from '../api';

export default function AccessCodeField(): React.ReactElement {
  /** Let a visitor enter the shared access code the server requires for building decks. */
  const [saved, setSaved] = useState<string>(getAccessCode());
  const [draft, setDraft] = useState<string>('');
  const [editing, setEditing] = useState<boolean>(!getAccessCode());

  function save(event: React.FormEvent): void {
    event.preventDefault();
    const code = draft.trim();
    setAccessCode(code);
    setSaved(code);
    setDraft('');
    setEditing(!code);
  }

  if (!editing && saved) {
    return (
      <p className="access-code muted">
        Access code saved.{' '}
        <button type="button" className="link-button" onClick={() => setEditing(true)}>
          Change
        </button>
      </p>
    );
  }

  return (
    <form className="access-code" onSubmit={save}>
      <label>
        Access code
        <input
          type="password"
          autoComplete="off"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Ask the site owner"
        />
      </label>
      <button type="submit" className="secondary" disabled={!draft.trim()}>
        Save
      </button>
    </form>
  );
}
