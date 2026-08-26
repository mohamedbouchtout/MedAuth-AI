# ADR-0012: track-a-clinical is the only issuer of session JWTs

**Status:** Accepted · **Task:** TASK-006

## Context

Phase 2's audio-ingestion task validated a "session JWT" that nothing in the
system created. The token had a validator and no issuer — a gap that would have
been filled locally, differently, by whichever service needed it first.

Something has to own session lifecycle. `track-a-clinical` owns the `encounters`
table, and a session *is* an encounter, so the row and the token should be
created by the same transaction.

## Decision

`services/track-a-clinical` owns session lifecycle and is the **only** minter of
session JWTs in the monorepo.

- `POST /sessions/start` creates the `encounters` row (`status='active'`), mints
  a JWT with claims exactly `{session_id, provider_id, exp}`, announces the
  session on `sessions:started`, and returns `{session_id, jwt}`.
- `POST /sessions/{session_id}/end` sets `status='completed'` and `ended_at`,
  and publishes `session:ended:{session_id}`.

`session_id` is generated server-side as a UUID, never client-supplied. Lifetime
comes from `SESSION_TTL_SECONDS` (default 900), never a hardcoded literal.
Signing is HS256 with `JWT_SIGNING_KEY` — symmetric is adequate for v1 because
every service is first-party.

**The claim set is deliberately minimal.** There are no `iss`/`aud` claims, even
though `.env.example` carries `JWT_ISSUER` and `JWT_AUDIENCE` from an earlier
scaffold pass. Adding a claim means changing both validators in the same change,
so the set stays this small until a deliberate hardening task widens it.

## Consequences

- Ending an already-completed session returns 200 and is idempotent, and does
  **not** publish a second Redis signal. TASK-030 and TASK-060 both react to
  that signal; a duplicate publish would mean duplicate SOAP generation or
  duplicate bundle assembly.
- A failed `sessions:started` publish is a **503** and the session is not
  usable. An encounter nobody is watching raises no nudges and looks exactly
  like an encounter with nothing to flag.
- Every real-time service validates and none of them mint. `audio-ingestion`
  and `nudge-service` each hold a mirror of the validation logic, not of the
  issuance logic.
- `TASK-006` is a hard prerequisite for TASK-020, TASK-021, TASK-030, TASK-041
  and TASK-060.

## References

- `services/track-a-clinical/src/track_a_clinical/session_tokens.py`, `api/sessions.py`
- `CLAUDE.md` -> Session Lifecycle & JWT Issuance
