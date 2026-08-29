import { describe, expect, it } from 'vitest';

import { SESSION_SUBPROTOCOL, sessionSubprotocols } from '../src/subprotocols';

describe('sessionSubprotocols', () => {
  it('offers the version marker first, so the server has something safe to echo', () => {
    // Selecting the medauth.jwt. entry would write the credential into the
    // handshake response headers and from there into every proxy log on the
    // path. The order is what stops a compliant server doing that.
    expect(sessionSubprotocols('a.b.c')[0]).toBe(SESSION_SUBPROTOCOL);
  });

  it('carries the token in the second entry', () => {
    expect(sessionSubprotocols('a.b.c')).toEqual([SESSION_SUBPROTOCOL, 'medauth.jwt.a.b.c']);
  });
});
