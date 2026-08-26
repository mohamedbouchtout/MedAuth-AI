# ADR-0013: A WebSocket accepts the session token from either of two carriers

**Status:** Accepted · **Task:** TASK-020; inherited by TASK-041 and TASK-023

## Context

`Authorization: Bearer <jwt>` is the obvious way to authenticate a WebSocket,
and it is what service-to-service callers and tests use.

It is not available to a browser. The native `WebSocket` constructor takes a URL
and a subprotocol list and nothing else — there is no header parameter. And
`apps/web` is required to use the native API rather than a library that tunnels
its own handshake, so this is a platform constraint rather than an
implementation gap to route around.

The third option a browser has is the URL query string, which is the one place a
credential is certain to be logged by intermediaries.

## Decision

Every real-time endpoint accepts the session token in **either** of two places,
and one is enough:

```
Authorization: Bearer <jwt>
Sec-WebSocket-Protocol: medauth.session.v1, medauth.jwt.<jwt>
```

Four rules make the carriers behave identically:

- **Validation is the same whichever carrier was used** — signature against
  `JWT_SIGNING_KEY`, `exp` in the future, and the token's `session_id` claim
  equal to the `session_id` in the URL path. Where the token arrived from is not
  an input to any check, so neither carrier can drift into being weaker.
- **Rejection happens before the handshake completes**, never after, so an
  unauthenticated peer never reaches a state where it can send a frame. The
  application close code is 4401.
- **The server echoes `medauth.session.v1` and never the token.** A browser
  aborts a connection whose handshake response does not name one of the
  subprotocols it offered, so the accept must select one — and selecting the
  `medauth.jwt.` entry would write the credential into the response headers and
  from there into every proxy access log on the path. The version marker is
  offered first precisely so the server has something safe to echo.
- **A token carried this way is still a credential.** Never logged, never in an
  error message, never in the query string.

## Consequences

- The 15-minute `SESSION_TTL_SECONDS` lifetime bounds the damage of a leaked
  token; TLS is what actually protects the handshake.
- **A close code cannot always be delivered.** Below the ASGI layer, a
  connection refused before the handshake completes has no WebSocket frame to
  carry a code in, so a real server answers the upgrade with an HTTP status. The
  4401 is what the application emits and what an ASGI-level test observes; a
  browser sees a failed upgrade rather than an `onclose` with 4401. Accepting an
  unauthenticated handshake purely so the rejection reads nicely would be worse.
- This is the canonical mechanism, not something TASK-020 settled locally.
  TASK-041's nudge socket inherits it by reference and TASK-023's browser capture
  must use the subprotocol form because nothing else is open to it.

## References

- `services/audio-ingestion/src/auth.py`, `api/websocket.py`
- `apps/web/src/hooks/useAudioCapture.ts`
- `CLAUDE.md` -> How the JWT reaches a WebSocket endpoint
