# Design: Session Lifecycle and JWT Issuance

**Service:** `services/track-a-clinical` · **Task:** TASK-006

Every real-time service in this system depends on a "session" existing, and for
a while nothing created one. Phase 2's audio-ingestion task validated a session
JWT that had no issuer. This is the piece that closes that gap, and it is a
prerequisite for TASK-020, TASK-021, TASK-030, TASK-041 and TASK-060.

## Why track-a-clinical owns it

A session **is** an encounter, and `track-a-clinical` owns the `encounters`
table. The row and the token should be created by the same request, so the
service that owns the row owns the lifecycle
([ADR-0012](../adr/0012-single-session-jwt-issuer.md)).

## `POST /sessions/start`

**Request:** `{patient_id, provider_id, ehr_encounter_id}`

What it does, in order:

1. Creates an `encounters` row with `status='active'`. The wire field
   `patient_id` maps to the model's `patient_fhir_id` column.
2. Generates `session_id` **server-side** as a UUID. Never client-supplied — the
   same rule as every other UUID in the schema.
3. Mints a JWT with claims exactly `{session_id, provider_id, exp}`.
4. **Publishes the `session_id` to `sessions:started`** — before the response
   returns.
5. Returns `{session_id, jwt}`.

**Response:** `{session_id, jwt}`

This is what `apps/web` and `apps/mobile` call when a provider taps "start
visit".

### The announcement, and why the ordering matters

Step 4 was added in TASK-021. A service consuming `transcription:{session_id}`
has to learn the session id from somewhere before it can subscribe to that exact
channel. The alternative was pattern-subscribing `transcription:*`, which puts a
wildcard across the one channel family carrying PHI and hands every consumer
every session's speech.

`sessions:started` is the single fixed channel in the canonical list, and the
only one with a payload — `{"session_id": ...}` — because the channel name has
no room for it.

**The ordering is safe by construction.** The publish precedes the response, and
no client can open its audio socket without the JWT from that response, so
nothing can be said before someone is listening.

**A failed publish is a 503 and the session is not usable.** An encounter nobody
watches raises no nudges and looks exactly like an encounter with nothing to
flag ([ADR-0028](../adr/0028-per-session-subscription.md)).

## `POST /sessions/{session_id}/end`

Sets `status='completed'` and `ended_at=NOW()`, then publishes
`session:ended:{session_id}` — an empty payload, because it is a signal, not a
data carrier.

Semantics:

| Case | Behaviour |
|---|---|
| Unknown or soft-deleted `session_id` | **404** |
| Active session | 200; status updated; signal published |
| Already completed | **200, idempotent — and no second publish** |

The last row is not tidiness. TASK-030 (SOAP generation) and TASK-060 (prior auth
bundle assembly) both subscribe to that signal, so a duplicate publish would
trigger duplicate SOAP generation or duplicate bundle assembly.

`track-b-rag`'s transcript consumer also subscribes, to unsubscribe from the
session's transcript channel and `DEL` its `procedure_seen:` set.

## The token

| Property | Value |
|---|---|
| Algorithm | **HS256** — symmetric is fine for v1; every service is first-party |
| Signing key | `JWT_SIGNING_KEY`, minimum 32 bytes |
| Claims | Exactly `{session_id, provider_id, exp}` |
| Lifetime | `SESSION_TTL_SECONDS`, default **900** (15 minutes) — never a literal |

**The claim set is deliberately minimal.** There are no `iss` or `aud` claims,
even though `.env.example` carries `JWT_ISSUER` and `JWT_AUDIENCE` sitting unused
from an earlier scaffold pass. Adding a claim means changing both validators
(TASK-020's and TASK-041's) in the same change, so the set stays this small until
a deliberate hardening task widens it.

`session_tokens.py` is the only place in the monorepo that mints one.
`audio-ingestion` and `nudge-service` each hold a mirror of the *validation*
logic, never of the issuance logic.

## How the token reaches a WebSocket

Either carrier, one is enough:

```
Authorization: Bearer <jwt>
Sec-WebSocket-Protocol: medauth.session.v1, medauth.jwt.<jwt>
```

The header is not available to a browser: the native `WebSocket` constructor
takes a URL and a subprotocol list and nothing else, and `apps/web` is required
to use the native API. That is a platform constraint, not an implementation gap
to route around, so the second carrier exists for it.

Four rules keep the carriers equivalent — validation is identical whichever was
used, rejection precedes the handshake, the server echoes the version marker and
never the token, and the token is never logged or placed in a URL. Full detail in
[ADR-0013](../adr/0013-two-websocket-token-carriers.md) and
[architecture/security-and-phi.md](../architecture/security-and-phi.md).

## Signals a session produces

| Channel | When | Consumed by |
|---|---|---|
| `sessions:started` | Before `/sessions/start` returns | track-b-rag |
| `transcription:{session_id}` | Per stabilized segment | track-a-clinical, track-b-rag |
| `nudges:{session_id}` | Per nudge *(TASK-040)* | nudge-service *(TASK-041)* |
| `session:ended:{session_id}` | On `/sessions/{id}/end` | track-a-clinical, track-b-rag, prior-auth |

Both rows and both signals key off `session_id`, not the encounter primary key:
that is the identifier already travelling through every Redis channel name.

## Known gap: restarts lose in-flight sessions

Watched sessions live in the consumer process, so a redeploy or a dropped Redis
connection mid-encounter leaves those visits unwatched until they end — no
keyword is detected and no nudge is raised, and the provider sees a quiet visit.

The consumer logs at WARNING when it happens, which is the honest version of the
problem rather than a fix. Rebuilding the watch set would mean querying
`encounters` for active rows on reconnect — real work with its own failure modes
— and it belongs with the task that makes this path produce actual queries.
TASK-030 makes the same trade for its transcript buffer, deliberately.

## Audit

`POST /sessions/start` and `POST /sessions/{id}/end` both touch PHI — they create
and close a row carrying `patient_fhir_id` — so both write an `audit_log` row
([ADR-0006](../adr/0006-audit-log-is-phi-only.md)). `GET /health` does not.
