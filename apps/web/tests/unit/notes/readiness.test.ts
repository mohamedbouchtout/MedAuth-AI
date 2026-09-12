/**
 * Telling a note that is not ready from a visit that does not exist (TASK-071).
 *
 * Both answers are 404, so the status alone decides nothing. TASK-032 made them
 * two error codes specifically so a review screen could tell a provider which
 * happened; a classifier that branched on the status would poll forever on an
 * unknown session, or give up on a note seconds away.
 */

import { describe, expect, it } from 'vitest';

import {
  classifyLoadFailure,
  NOT_GENERATED_CODE,
  POLL_MAX_ATTEMPTS,
  SESSION_NOT_FOUND_CODE,
  shouldPollAgain,
} from '../../../src/notes/readiness';

function status(code: string, statusCode = 404) {
  return { kind: 'status' as const, status: statusCode, code, message: 'm' };
}

describe('classifying a failed read', () => {
  it('polls on a note that has not been generated yet', () => {
    expect(classifyLoadFailure(status(NOT_GENERATED_CODE))).toEqual({ kind: 'pending' });
  });

  /** Asking again will not make an unknown session known. */
  it('stops on an unknown session, though the status is the same', () => {
    expect(classifyLoadFailure(status(SESSION_NOT_FOUND_CODE))).toEqual({ kind: 'missing' });
  });

  it('stops on a network failure rather than treating it as progress', () => {
    const failure = { kind: 'network' as const, message: 'unreachable' };

    expect(classifyLoadFailure(failure)).toEqual({ kind: 'error', failure });
  });

  it('stops on any other status', () => {
    const failure = status('something_else', 500);

    expect(classifyLoadFailure(failure)).toEqual({ kind: 'error', failure });
  });
});

describe('the poll ceiling', () => {
  it('keeps asking below the limit', () => {
    expect(shouldPollAgain(0)).toBe(true);
    expect(shouldPollAgain(POLL_MAX_ATTEMPTS - 1)).toBe(true);
  });

  /**
   * A generation that failed outright answers exactly as one still in progress,
   * so a screen that polled forever would present a permanent failure as
   * perpetual progress.
   */
  it('gives up at the limit', () => {
    expect(shouldPollAgain(POLL_MAX_ATTEMPTS)).toBe(false);
  });
});
