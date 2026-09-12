/**
 * Telling "the note isn't ready" apart from "this visit does not exist", and
 * deciding whether to ask again (TASK-071).
 *
 * **Why this exists at all.** The review screen is reached by ending a visit,
 * and TASK-030 generates the note from the `session:ended` signal through a
 * Sonnet call — so the first `GET /notes/{session_id}` will usually answer 404
 * `note_not_generated`. TASK-032 made that a different error code from
 * `session_not_found` specifically so a review screen could tell a provider
 * which of the two happened, and collapsing them back into one "not found"
 * screen here would discard the distinction at the only place it was ever meant
 * to be used.
 *
 * The same 404 status carries both, so the code is the whole signal. A caller
 * that branched on the status alone would poll forever on a session that does
 * not exist, or give up on one whose note is seconds away.
 */

import type { ApiFailure } from '@medauth/session-client';

/** The note has not been generated yet. The ordinary state after ending a visit. */
export const NOT_GENERATED_CODE = 'note_not_generated';

/** No such session, or it has been soft-deleted. Asking again will not change that. */
export const SESSION_NOT_FOUND_CODE = 'session_not_found';

/**
 * How often the screen asks again while a note is still being generated.
 *
 * **A chosen default rather than a measurement.** Nobody has timed a Sonnet SOAP
 * generation over a long encounter transcript, so this is a round number picked
 * to be frequent enough that the note appears to arrive on its own and sparse
 * enough not to hammer the service. Changing it is safe; do not read it as a
 * figure derived from anything.
 */
export const POLL_INTERVAL_MS = 3_000;

/**
 * How many times, before the screen stops and offers a manual retry.
 *
 * Forty attempts at three seconds is two minutes. **Also a chosen default**, and
 * the ceiling exists for a reason the interval does not: a generation that
 * failed outright produces exactly the same `note_not_generated` answer as one
 * still in progress, and a screen that polls indefinitely presents a permanent
 * failure as perpetual progress. Giving up and saying so is the honest end
 * state, and the manual retry is there because the provider may know the note
 * matters more than this app does.
 */
export const POLL_MAX_ATTEMPTS = 40;

/** What a failed read means for the screen. */
export type LoadOutcome =
  /** The note is being generated. Ask again. */
  | { kind: 'pending' }
  /** No such visit. Asking again will not change that. */
  | { kind: 'missing' }
  /** Something else went wrong; the provider may retry by hand. */
  | { kind: 'error'; failure: ApiFailure };

/**
 * Classify a failed read.
 *
 * Only `note_not_generated` polls. Everything else — including a 404 whose code
 * is `session_not_found`, and including a network failure — stops, because
 * repeating a question that was answered definitively is not a retry.
 */
export function classifyLoadFailure(failure: ApiFailure): LoadOutcome {
  if (failure.kind === 'status' && failure.code === NOT_GENERATED_CODE) {
    return { kind: 'pending' };
  }
  if (failure.kind === 'status' && failure.code === SESSION_NOT_FOUND_CODE) {
    return { kind: 'missing' };
  }
  return { kind: 'error', failure };
}

/** Whether a pending note is worth asking about again, given how often we have. */
export function shouldPollAgain(attempts: number): boolean {
  return attempts < POLL_MAX_ATTEMPTS;
}
