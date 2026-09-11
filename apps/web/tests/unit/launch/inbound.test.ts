import { describe, expect, it, vi } from 'vitest';

import { CLAIM_PARAM, readClaim, scrubClaim } from '../../../src/launch/inbound';

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

describe('scrubClaim', () => {
  it('removes the claim without touching the path', () => {
    const history = { replaceState: vi.fn() } as unknown as History;

    scrubClaim(history, locationWith('/launch', `?${CLAIM_PARAM}=abc-123`));

    expect(history.replaceState).toHaveBeenCalledWith(null, '', '/launch');
  });

  it('keeps every other parameter', () => {
    const history = { replaceState: vi.fn() } as unknown as History;

    scrubClaim(history, locationWith('/launch', '?tab=notes&claim=abc-123'));

    expect(history.replaceState).toHaveBeenCalledWith(null, '', '/launch?tab=notes');
  });

  /**
   * Nothing to remove is not a reason to rewrite the URL. A `replaceState` on
   * every load would be a no-op the browser still records, and would fire on
   * pages this module has no business touching.
   */
  it('does nothing when there is no claim', () => {
    const history = { replaceState: vi.fn() } as unknown as History;

    scrubClaim(history, locationWith('/', '?tab=notes'));

    expect(history.replaceState).not.toHaveBeenCalled();
  });
});
