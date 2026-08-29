/**
 * Reading the `exp` claim out of a session token, for proactive refresh.
 *
 * CLAUDE.md's "Refresh proactively and reactively" rule asks a client to refresh
 * before opening a new socket when the token it holds is close to `exp`, and
 * observes that this costs nothing because the client already holds `exp` — it
 * is a claim in the token it was given.
 *
 * Three things this module is not:
 *
 * - **It is not validation.** Nothing here checks a signature, and no decision
 *   about whether a token is *acceptable* may be made from it. The server is the
 *   only thing that validates a session token; this is a scheduling hint.
 * - **It does not log.** The decoded payload is the contents of a credential.
 * - **It does not use `atob`.** Hermes does not guarantee it, so the mobile app
 *   could not rely on it; a browser has it, but one decoder shared by both
 *   consumers is worth more than a runtime check that picks between two. A
 *   base64 decoder is fifteen lines. Failing to decode is handled, never thrown.
 */

/** Standard base64 alphabet; base64url's two substitutions are mapped on the way in. */
const BASE64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

/**
 * How close to `exp` counts as "close enough to refresh first".
 *
 * **A round-number default, not a measured value**, in the same sense as
 * `FIRST_AUDIO_TIMEOUT_MS` and `MAX_PENDING_BYTES`. It needs to exceed the time
 * between the check and the handshake completing — one HTTP round trip and a
 * socket upgrade — with enough margin that a slow network does not land a token
 * that was fine at check time and expired in flight. A minute is comfortably
 * above that on any network a clinic would use, and well under the 15-minute
 * `SESSION_TTL_SECONDS`, so it does not turn every visit into a refresh.
 */
export const TOKEN_REFRESH_SKEW_MS = 60_000;

function decodeBase64Url(segment: string): string | null {
  const normalized = segment.replace(/-/g, '+').replace(/_/g, '/');
  let bits = 0;
  let bitCount = 0;
  let out = '';

  for (const char of normalized) {
    if (char === '=') {
      break;
    }
    const value = BASE64_ALPHABET.indexOf(char);
    if (value < 0) {
      return null;
    }
    bits = (bits << 6) | value;
    bitCount += 6;
    if (bitCount >= 8) {
      bitCount -= 8;
      out += String.fromCharCode((bits >> bitCount) & 0xff);
    }
  }

  return out;
}

/**
 * The token's `exp` claim in milliseconds, or null when it cannot be read.
 *
 * Null covers every malformed shape — wrong segment count, undecodable payload,
 * payload that is not JSON, missing or non-numeric `exp` — because the caller
 * does the same thing in all of them, and telling them apart would mean
 * describing a credential's contents.
 */
export function expiresAtMs(jwt: string): number | null {
  const payload = jwt.split('.')[1];
  if (payload === undefined || payload === '') {
    return null;
  }

  const decoded = decodeBase64Url(payload);
  if (decoded === null) {
    return null;
  }

  let claims: unknown;
  try {
    claims = JSON.parse(decoded);
  } catch {
    return null;
  }

  if (typeof claims !== 'object' || claims === null) {
    return null;
  }
  const exp = (claims as { exp?: unknown }).exp;
  return typeof exp === 'number' && Number.isFinite(exp) ? exp * 1000 : null;
}

/**
 * Whether this token should be refreshed before it is used to open a socket.
 *
 * An unreadable token counts as near expiry. It cannot open a socket in any
 * case, and routing it through the re-mint endpoint turns a vague
 * `AUTH_REJECTED` from a failed upgrade into the specific answer the endpoint
 * gives — 401, 404 or the 409 that means the visit is genuinely over.
 */
export function isNearExpiry(jwt: string, now: number, skewMs = TOKEN_REFRESH_SKEW_MS): boolean {
  const expiresAt = expiresAtMs(jwt);
  return expiresAt === null || expiresAt - now <= skewMs;
}
