/**
 * Base64url encoding for tests, written out rather than taken from `Buffer`.
 *
 * Nothing in `src/` runs on Node, and reaching for a Node global to build a test
 * fixture is how one ends up typechecking inside the app. Twenty lines here is
 * the cheaper trade.
 *
 * This is the encoding side of what `@medauth/session-client`'s `expiresAtMs`
 * decodes, kept independent of it so a bug in the decoder cannot be masked by
 * the same bug in the fixture. That package's own suite carries a matching copy
 * for the same reason — see the note there on why a shared *decoder* matters and
 * a shared test fixture does not.
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
