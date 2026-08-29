/**
 * How a session token reaches a WebSocket endpoint from a client that cannot set
 * headers.
 *
 * CLAUDE.md, "How the JWT reaches a WebSocket endpoint", defines two carriers and
 * says a server accepts either. The header is the obvious one and is what
 * service-to-service callers use; it is not available to a browser, because the
 * native `WebSocket` constructor takes a URL and a subprotocol list and nothing
 * else. That is a platform constraint rather than an implementation gap, which
 * is why the second carrier exists at all.
 *
 * So this is `apps/web`'s carrier on every real-time endpoint — the audio socket
 * and the nudge stream both. `apps/mobile` uses the header instead: React
 * Native's `WebSocket` takes a third options argument the DOM's does not.
 *
 * Two properties of the list are load-bearing rather than cosmetic:
 *
 * - **The version marker is offered first**, so the server has something safe to
 *   echo. A browser aborts a connection whose handshake response does not name
 *   one of the subprotocols it offered, so the accept must select one — and
 *   selecting the `medauth.jwt.` entry would write the credential into the
 *   response headers and from there into every proxy access log on the path.
 * - **The token is never put in the query string**, which is the third thing a
 *   browser can carry and the one place a credential is certain to be logged by
 *   intermediaries.
 *
 * It lives here rather than in `apps/web` because this package already owns how
 * a session token is presented — the `Authorization: Bearer` carrier the re-mint
 * call uses is one module over — and because the ordering rule above is a
 * property of the token, not of either hook that builds a list. Two hooks in one
 * app were enough to make a second copy possible; the rule is that the token is
 * never first, and one function is what enforces it.
 */

/** The version marker the server echoes. Offered first, always. */
export const SESSION_SUBPROTOCOL = 'medauth.session.v1';

/** The subprotocol list for a session-authenticated WebSocket. */
export function sessionSubprotocols(jwt: string): string[] {
  return [SESSION_SUBPROTOCOL, `medauth.jwt.${jwt}`];
}
