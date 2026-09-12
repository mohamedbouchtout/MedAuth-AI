/**
 * Waiting for a note that is still being generated (TASK-071).
 *
 * The screen is reached by ending a visit, and TASK-030 generates the note from
 * the `session:ended` signal through a Sonnet call — so the first read usually
 * answers `note_not_generated`. These tests are about what the hook does with
 * that: it asks again, it stops asking on an answer that will not change, and it
 * stops asking eventually even on one that might.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useNote } from '../../../src/hooks/useNote';
import { POLL_INTERVAL_MS, POLL_MAX_ATTEMPTS } from '../../../src/notes/readiness';
import { aNote, notesFailing, notesServing, SESSION_ID, type FakeNotes } from '../../support/notes';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

/**
 * Let every pending promise and every due timer run.
 *
 * `waitFor` is deliberately not used in this file: it polls on real timers, and
 * every assertion here is about a hook driven by fake ones. Advancing the clock
 * explicitly is also the more honest test — it says how much time passed.
 */
async function settle(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Always "not generated yet", so the poll never resolves on its own. */
function neverReady(): FakeNotes {
  return notesFailing(404, 'note_not_generated', 'still generating');
}

describe('loading a note', () => {
  it('reports the note once it is there', async () => {
    const notes = notesServing(aNote());
    const { result } = renderHook(() => useNote(SESSION_ID, notes));

    await settle();

    expect(result.current.state.kind).toBe('loaded');
    expect(notes.readNote).toHaveBeenCalledWith(SESSION_ID);
  });

  it('polls while the note is still being generated', async () => {
    const notes = neverReady();
    renderHook(() => useNote(SESSION_ID, notes));

    await settle();
    expect(notes.readNote).toHaveBeenCalledTimes(1);

    await settle(POLL_INTERVAL_MS);

    expect(notes.readNote).toHaveBeenCalledTimes(2);
  });

  /**
   * An unknown session does not become known by asking again. This is the whole
   * reason TASK-032 answers two different codes on one status.
   */
  it('does not poll an unknown session', async () => {
    const notes = notesFailing(404, 'session_not_found');
    const { result } = renderHook(() => useNote(SESSION_ID, notes));

    await settle();
    expect(result.current.state.kind).toBe('missing');

    await settle(POLL_INTERVAL_MS * 3);

    expect(notes.readNote).toHaveBeenCalledTimes(1);
  });

  it('does not poll a transport failure', async () => {
    const notes: FakeNotes = {
      readNote: vi.fn(() =>
        Promise.resolve({ ok: false as const, failure: { kind: 'network' as const, message: 'x' } }),
      ),
      updateNote: vi.fn(),
    };
    const { result } = renderHook(() => useNote(SESSION_ID, notes));

    await settle();
    expect(result.current.state.kind).toBe('error');

    await settle(POLL_INTERVAL_MS * 3);

    expect(notes.readNote).toHaveBeenCalledTimes(1);
  });

  /**
   * A generation that failed outright answers exactly as one still running, so
   * the poll has to end. `exhausted` is what lets the screen say so instead of
   * presenting a permanent failure as perpetual progress.
   */
  it('gives up after the ceiling and reports that it has', async () => {
    const notes = neverReady();
    const { result } = renderHook(() => useNote(SESSION_ID, notes));

    await settle(POLL_INTERVAL_MS * (POLL_MAX_ATTEMPTS + 5));

    expect(notes.readNote).toHaveBeenCalledTimes(POLL_MAX_ATTEMPTS);
    expect(result.current.state).toEqual({
      kind: 'pending',
      attempts: POLL_MAX_ATTEMPTS,
      exhausted: true,
    });
  });

  it('asks again from the top on a manual retry', async () => {
    const notes = neverReady();
    const { result } = renderHook(() => useNote(SESSION_ID, notes));

    await settle(POLL_INTERVAL_MS * (POLL_MAX_ATTEMPTS + 5));
    expect(notes.readNote).toHaveBeenCalledTimes(POLL_MAX_ATTEMPTS);

    act(() => result.current.retry());
    await settle();

    expect(notes.readNote.mock.calls.length).toBeGreaterThan(POLL_MAX_ATTEMPTS);
  });

  /** Stops the timer, so a screen that has gone away does not keep asking. */
  it('stops polling when unmounted', async () => {
    const notes = neverReady();
    const { unmount } = renderHook(() => useNote(SESSION_ID, notes));

    await settle();
    expect(notes.readNote).toHaveBeenCalledTimes(1);

    unmount();
    await settle(POLL_INTERVAL_MS * 3);

    expect(notes.readNote).toHaveBeenCalledTimes(1);
  });
});
