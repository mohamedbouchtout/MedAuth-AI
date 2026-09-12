/**
 * An editable list of extracted clinical codes (TASK-071).
 *
 * **A machine suggestion is never mixed indistinguishably into the list a
 * provider is signing.** CLAUDE.md's shape contract requires it: the same
 * Comprehend Medical request that validates the LLM's codes also surfaces codes
 * it never proposed, and those are suggestions rather than stated diagnoses.
 * They are rendered as their own thing, with their own action — accepting one
 * rewrites its `source` to `provider-accepted`, which is the mechanism by which
 * a suggestion becomes documentation a prior-auth bundle may claim (TASK-060)
 * and the only way one may ever reach a patient's chart (TASK-053).
 *
 * **`null` and `[]` are rendered differently, and that is not decoration.**
 * `null` means the extraction pass never answered; `[]` means it ran and found
 * nothing. Showing the first as "no diagnoses" is the collapse the contract
 * exists to prevent, arriving through a UI instead of through a request body.
 *
 * Nothing here logs: a diagnosis code is clinical content.
 */

import { useState } from 'react';

import type { ExtractedCode } from '../api/notes';

export interface CodeListProps {
  /** Shown as the section heading, e.g. "ICD-10 diagnoses". */
  title: string;
  /** Distinguishes this list's test ids and input labels from the other's. */
  name: string;
  codes: ExtractedCode[] | null;
  /** Accepting a suggestion, which only this list's owner knows how to apply. */
  onAccept: (code: string) => void;
  onRemove: (code: string) => void;
  onAdd: (code: string) => void;
}

const SOURCE_LABELS: Record<ExtractedCode['source'], string> = {
  'llm-extraction': 'Extracted from the encounter',
  'comprehend-medical': 'Suggested — not yet accepted',
  'provider-accepted': 'Accepted by you',
};

function CodeRow({
  entry,
  name,
  onAccept,
  onRemove,
}: {
  entry: ExtractedCode;
  name: string;
  onAccept: (code: string) => void;
  onRemove: (code: string) => void;
}) {
  const suggestion = entry.source === 'comprehend-medical';
  return (
    <li
      data-testid={`${name}-entry-${entry.code}`}
      data-source={entry.source}
      className={`flex items-center gap-3 rounded-md border px-3 py-2 ${
        suggestion ? 'border-dashed border-amber-400 bg-amber-50' : 'border-slate-200 bg-white'
      }`}
    >
      <span className="font-mono text-sm font-semibold text-slate-900">{entry.code}</span>
      {entry.display !== null && <span className="text-sm text-slate-700">{entry.display}</span>}
      <span
        className={`ml-auto text-xs ${suggestion ? 'font-semibold text-amber-800' : 'text-slate-500'}`}
        data-testid={`${name}-source-${entry.code}`}
      >
        {SOURCE_LABELS[entry.source]}
      </span>
      {suggestion && (
        <button
          type="button"
          onClick={() => onAccept(entry.code)}
          data-testid={`${name}-accept-${entry.code}`}
          className="rounded-md bg-slate-900 px-2 py-1 text-xs font-semibold text-white hover:bg-slate-700"
        >
          Accept
        </button>
      )}
      <button
        type="button"
        onClick={() => onRemove(entry.code)}
        data-testid={`${name}-remove-${entry.code}`}
        className="rounded-md border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-100"
      >
        Remove
      </button>
    </li>
  );
}

export function CodeList({ title, name, codes, onAccept, onRemove, onAdd }: CodeListProps) {
  const [entry, setEntry] = useState('');

  function add() {
    const trimmed = entry.trim();
    if (trimmed === '') {
      return;
    }
    onAdd(trimmed);
    setEntry('');
  }

  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold text-slate-900">{title}</h3>

      {codes === null ? (
        // Not "no diagnoses". The extraction pass never produced an answer for
        // this note, which is a different fact and is the one worth saying.
        <p className="text-sm text-slate-600" data-testid={`${name}-unanswered`}>
          No codes were extracted for this note — the extraction step did not produce an answer.
          Anything added here is recorded as your own.
        </p>
      ) : codes.length === 0 ? (
        <p className="text-sm text-slate-600" data-testid={`${name}-empty`}>
          The extraction ran and found no codes.
        </p>
      ) : (
        <ul className="flex flex-col gap-2" data-testid={`${name}-list`}>
          {codes.map((code) => (
            <CodeRow
              key={code.code}
              entry={code}
              name={name}
              onAccept={onAccept}
              onRemove={onRemove}
            />
          ))}
        </ul>
      )}

      <div className="flex items-center gap-2">
        <label className="sr-only" htmlFor={`${name}-add`}>
          Add a code to {title}
        </label>
        <input
          id={`${name}-add`}
          data-testid={`${name}-add-input`}
          value={entry}
          onChange={(event) => setEntry(event.target.value)}
          placeholder="Add a code"
          className="rounded-md border border-slate-300 px-2 py-1 font-mono text-sm"
        />
        <button
          type="button"
          onClick={add}
          data-testid={`${name}-add`}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-100"
        >
          Add
        </button>
      </div>
    </section>
  );
}
