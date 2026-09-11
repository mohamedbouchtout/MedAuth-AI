import { describe, expect, it, vi } from 'vitest';

import {
  CLAIM_PARAM,
  ERROR_PARAM,
  readClaim,
  readLaunchFailure,
  scrubLaunchParams,
} from '../../../src/launch/inbound';

/**
 * Recognising a completed launch in this app's own URL (TASK-070).
 *
 * Two properties matter. A claim code must be found wherever the service's
 * `SMART_WEB_RETURN_URL` put it, because this app holds no copy of that setting
 * and a path comparison could only ever disagree with it. And a code must be out
 * of the URL once read: it is a credential, and the address bar and session
 * history are both places browsers are built to remember.
 */

function locationWith(pathname: string, search: string): Location {
  return { pathname, search } as Location;
}

describe('readClaim', () => {
  it('reads the claim the callback appended', () => {
    expect(readClaim('?claim=abc-123')).toBe('abc-123');
  });

  it('reads it alongside other parameters, in any position', () => {
    expect(readClaim('?state=x&claim=abc-123&other=y')).toBe('abc-123');
  });

  /**
   * The ordinary case: every load of this app that did not follow a launch.
   * Null rather than an error, because there is nothing wrong with it.
   */
  it('answers null for a plain load', () => {
    expect(readClaim('')).toBeNull();
    expect(readClaim('?tab=notes')).toBeNull();
  });

  /**
   * An empty `?claim=` names no launch. Redeeming it would spend a request to be
   * told 404 by a route that is right to answer that way, and the provider would
   * see a failed launch rather than a plain page load.
   */
  it('treats an empty claim as absent', () => {
    expect(readClaim('?claim=')).toBeNull();
  });
});

/**
 * A launch that came back having failed (TASK-051g).
 *
 * The parameter is the only thing that distinguishes a refused sign-in from a
 * plain load of this app, which is what made the two indistinguishable before
 * the service delivered failures at all.
 */
describe('readLaunchFailure', () => {
  it('reads a decline', () => {
    expect(readLaunchFailure(`?${ERROR_PARAM}=declined`)).toBe('declined');
  });

  it('reads a generic failure', () => {
    expect(readLaunchFailure('?error=failed')).toBe('failed');
  });

  it('reads it alongside other parameters, in any position', () => {
    expect(readLaunchFailure('?tab=notes&error=declined')).toBe('declined');
  });

  it('answers null for a plain load', () => {
    expect(readLaunchFailure('')).toBeNull();
    expect(readLaunchFailure('?tab=notes')).toBeNull();
    expect(readLaunchFailure('?error=')).toBeNull();
  });

  /**
   * The service may add a member this build does not know. Reading it as "no
   * failure" would put the provider back on the sign-in screen with nothing
   * said, which is the silence this whole delivery exists to end — so what is
   * certain from an `error` parameter, that the launch did not complete, is
   * what is reported.
   */
  it('narrows a value it does not recognise to the generic failure', () => {
    expect(readLaunchFailure('?error=some_future_member')).toBe('failed');
  });

  /**
   * A completed launch carries a claim and no error; a failed one carries an
   * error and no claim. Neither reader may see the other's parameter.
   */
  it('does not read a claim as a failure, and vice versa', () => {
    expect(readLaunchFailure('?claim=abc-123')).toBeNull();
    expect(readClaim('?error=declined')).toBeNull();
  });
});

describe('scrubLaunchParams', () => {
  it('removes the claim without touching the path', () => {
    const history = { replaceState: vi.fn() } as unknown as History;

    scrubLaunchParams(history, locationWith('/launch', `?${CLAIM_PARAM}=abc-123`));

    expect(history.replaceState).toHaveBeenCalledWith(null, '', '/launch');
  });

  it('keeps every other parameter', () => {
    const history = { replaceState: vi.fn() } as unknown as History;

    scrubLaunchParams(history, locationWith('/launch', '?tab=notes&claim=abc-123'));

    expect(history.replaceState).toHaveBeenCalledWith(null, '', '/launch?tab=notes');
  });

  /**
   * An `error` is not a credential, but it is a report of one moment: left in
   * the URL, a reload would replay a failure the provider has already seen.
   */
  it('removes a reported failure too', () => {
    const history = { replaceState: vi.fn() } as unknown as History;

    scrubLaunchParams(history, locationWith('/launch', `?${ERROR_PARAM}=declined`));

    expect(history.replaceState).toHaveBeenCalledWith(null, '', '/launch');
  });

  /**
   * Nothing to remove is not a reason to rewrite the URL. A `replaceState` on
   * every load would be a no-op the browser still records, and would fire on
   * pages this module has no business touching.
   */
  it('does nothing when there is no claim', () => {
    const history = { replaceState: vi.fn() } as unknown as History;

    scrubLaunchParams(history, locationWith('/', '?tab=notes'));

    expect(history.replaceState).not.toHaveBeenCalled();
  });
});
