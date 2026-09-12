/**
 * Loading a session's note, and waiting for one that is still being generated
 * (TASK-071).
 *
 * **The wait is the point.** This screen is reached by ending a visit, and
 * TASK-030 generates the note from the `session:ended` signal through a Sonnet
 * call — so the first read usually answers 404 `note_not_generated`. Asking the
 * provider to refresh a page whose content arrives on its own within seconds is
 * work this app can do instead. What it must not do is poll on an answer that
 * will not change: an unknown session does not become known by asking again, and
 * `readiness` is where that distinction lives.
 *
 * Nothing here logs: the value being fetched is a patient's note.
 */

import type { ApiFailure } from '@medauth/session-client';
import { useCallback, useEffect, useState } from 'react';

import { notesApi, type Note, type NotesApi } from '../api/notes';
import { classifyLoadFailure, POLL_INTERVAL_MS, shouldPollAgain } from '../notes/readiness';

/** Where the load is. */
export type NoteLoad =
  /** The first read is in flight. */
  | { kind: 'loading' }
  /**
   * The note is being generated.
   *
   * `exhausted` means the poll gave up — which is not the same as the note
   * having failed, and is worded that way to the provider. A manual retry is
   * offered rather than the screen quietly continuing forever.
   */
  | { kind: 'pending'; attempts: number; exhausted: boolean }
  /** No such visit. */
  | { kind: 'missing' }
  /** Something else went wrong. Retrying by hand is reasonable. */
  | { kind: 'error'; failure: ApiFailure }
  | { kind: 'loaded'; note: Note };

export interface UseNote {
  state: NoteLoad;
  /** Ask again now, from the top. */
  retry: () => void;
  /** Put an updated note in place after a save or a successful write-back. */
  replace: (note: Note) => void;
}

export function useNote(sessionId: string, notes: NotesApi = notesApi): UseNote {
  const [state, setState] = useState<NoteLoad>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);

  /**
   * Which session the held state describes.
   *
   * The reset happens during render rather than in the effect below, which is
   * React's own recommendation for adjusting state when a prop changes — and
   * here it is also what stops a screen pointed at a new session from showing
   * the previous one's note for a frame.
   */
  const [loadedFor, setLoadedFor] = useState(sessionId);
  if (loadedFor !== sessionId) {
    setLoadedFor(sessionId);
    setState({ kind: 'loading' });
  }

  const retry = useCallback(() => {
    setState({ kind: 'loading' });
    setAttempt((current) => current + 1);
  }, []);
  const replace = useCallback((note: Note) => setState({ kind: 'loaded', note }), []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let polls = 0;

    async function read(): Promise<void> {
      const result = await notes.readNote(sessionId);
      if (cancelled) {
        return;
      }
      if (result.ok) {
        setState({ kind: 'loaded', note: result.value });
        return;
      }

      const outcome = classifyLoadFailure(result.failure);
      if (outcome.kind === 'missing') {
        setState({ kind: 'missing' });
        return;
      }
      if (outcome.kind === 'error') {
        setState({ kind: 'error', failure: outcome.failure });
        return;
      }

      polls += 1;
      const again = shouldPollAgain(polls);
      setState({ kind: 'pending', attempts: polls, exhausted: !again });
      if (again) {
        timer = setTimeout(() => void read(), POLL_INTERVAL_MS);
      }
    }

    void read();

    return () => {
      cancelled = true;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    };
  }, [sessionId, notes, attempt]);

  return { state, retry, replace };
}
