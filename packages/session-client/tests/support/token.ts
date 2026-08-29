/**
 * Base64url encoding for tests, written out rather than taken from `Buffer`.
 *
 * This package has no `@types/node` and no reason to acquire one — nothing in
 * `src/` runs on Node, and adding the types to build a test fixture would let
 * Node-only globals typecheck inside the package itself. Twenty lines here is
 * the cheaper trade.
 *
 * This is the encoding side of what `src/jwt.ts` decodes, kept independent of it
 * so a bug in the decoder cannot be masked by the same bug in the fixture.
 *
 * `apps/mobile/tests/support/token.ts` is a second copy, and deliberately so:
 * that app's session-screen test builds tokens of its own, and a fixture reached
 * across a package boundary would make this package's test tree part of its
 * published surface. Two test fixtures drifting apart costs a failing test; one
 * shared decoder drifting apart costs a credential handled two ways, which is
 * why `src/jwt.ts` moved here and this file did not.
 */

const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';

export function encodeBase64Url(text: string): string {
  let out = '';

  for (let index = 0; index < text.length; index += 3) {
    const first = text.charCodeAt(index);
    const hasSecond = index + 1 < text.length;
    const hasThird = index + 2 < text.length;
    const second = hasSecond ? text.charCodeAt(index + 1) : 0;
    const third = hasThird ? text.charCodeAt(index + 2) : 0;

    out += ALPHABET.charAt(first >> 2);
    out += ALPHABET.charAt(((first & 0b11) << 4) | (second >> 4));
    if (hasSecond) {
      out += ALPHABET.charAt(((second & 0b1111) << 2) | (third >> 6));
    }
    if (hasThird) {
      out += ALPHABET.charAt(third & 0b111111);
    }
  }

  return out;
}

/** A token shaped like a session JWT, carrying the claims given. No signature is real. */
export function tokenWithClaims(claims: Record<string, unknown>): string {
  return `header.${encodeBase64Url(JSON.stringify(claims))}.signature`;
}

/** A token expiring `secondsFromNow` after the `now` passed in, in epoch seconds. */
export function tokenExpiringAt(nowMs: number, secondsFromNow: number): string {
  return tokenWithClaims({ session_id: 'test', exp: Math.floor(nowMs / 1000) + secondsFromNow });
}
