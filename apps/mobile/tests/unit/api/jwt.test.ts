import { TOKEN_REFRESH_SKEW_MS, expiresAtMs, isNearExpiry } from '../../../src/api/jwt';
import { encodeBase64Url, tokenWithClaims } from '../../support/token';

/** A token whose payload carries the given `exp`, in seconds since the epoch. */
function tokenWithExp(expSeconds: number): string {
  return tokenWithClaims({ session_id: 'x', exp: expSeconds });
}

describe('expiresAtMs', () => {
  it('reads the exp claim in milliseconds', () => {
    expect(expiresAtMs(tokenWithExp(1_700_000_000))).toBe(1_700_000_000_000);
  });

  it('decodes a payload using base64url characters', () => {
    // '-' and '_' stand in for '+' and '/'; a decoder that only handles standard
    // base64 rejects roughly one token in a handful, which would look like a
    // flaky refresh rather than a decoding bug.
    const payload = encodeBase64Url(JSON.stringify({ pad: '<<~~??>>', exp: 42 }));
    expect(payload).toMatch(/[-_]/);
    expect(expiresAtMs(`header.${payload}.signature`)).toBe(42_000);
  });

  it.each([
    ['not a jwt at all', 'opaque-token'],
    ['a payload that is not base64', 'header.***.signature'],
    ['a payload that is not JSON', `header.${encodeBase64Url('nonsense')}.sig`],
    ['a payload with no exp', `header.${encodeBase64Url('{"a":1}')}.sig`],
    ['a non-numeric exp', `header.${encodeBase64Url('{"exp":"soon"}')}.sig`],
  ])('returns null for %s', (_label, jwt) => {
    expect(expiresAtMs(jwt)).toBeNull();
  });
});

describe('isNearExpiry', () => {
  const now = 1_700_000_000_000;

  it('is false for a token with plenty of life left', () => {
    expect(isNearExpiry(tokenWithExp(now / 1000 + 600), now)).toBe(false);
  });

  it('is true inside the refresh skew', () => {
    expect(isNearExpiry(tokenWithExp(now / 1000 + 30), now)).toBe(true);
  });

  it('is true for a token that has already expired', () => {
    expect(isNearExpiry(tokenWithExp(now / 1000 - 30), now)).toBe(true);
  });

  it('treats an unreadable token as near expiry', () => {
    // It cannot open a socket either way, and routing it through the re-mint
    // endpoint turns a vague failed upgrade into 401/404/409 — an answer.
    expect(isNearExpiry('opaque-token', now)).toBe(true);
  });

  it('uses a skew shorter than the 15-minute session lifetime', () => {
    // Otherwise every visit would begin by refreshing the token it was just given.
    expect(TOKEN_REFRESH_SKEW_MS).toBeLessThan(900_000);
  });
});
