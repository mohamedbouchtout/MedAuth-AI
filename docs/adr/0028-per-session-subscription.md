# ADR-0028: Consumers subscribe per session, announced on `sessions:started`

**Status:** Accepted · **Task:** TASK-021 (channel added to TASK-006)

## Context

`transcription:{session_id}` carries the speech of a clinical encounter. A
consumer has to learn a session exists before it can subscribe to that channel
**by name**.

The alternative is `PSUBSCRIBE transcription:*` — a wildcard across the one
channel family in the system that carries PHI, handing every consumer every
session's speech regardless of whether it has any business with it.

But a per-session subscription creates an ordering hazard. If a consumer
subscribes after the first segment is published, that segment is lost, and under
Redis pub/sub it is lost silently and permanently.

## Decision

`POST /sessions/start` publishes the new `session_id` to the single fixed
channel `sessions:started`, carrying `{"session_id": ...}` as its payload —
the one channel in the canonical list with a payload, because the channel name
has no room for the id.

Consumers subscribe to `sessions:started`, and on each announcement subscribe to
that session's `transcription:{session_id}` and `session:ended:{session_id}`.

**The ordering is safe by construction.** The announcement is published *before*
`/sessions/start` returns the JWT, and no client can open the audio socket
without that token, so nothing can be said before someone is listening. A failed
publish is a **503** and the session is not usable — an encounter nobody watches
raises no nudges and looks exactly like an encounter with nothing to flag.

## Consequences

- No wildcard subscription exists anywhere in the system.
- A consumer sees only the sessions announced while it was running. **A restart
  loses the sessions in flight**: watched sessions live in the consumer process,
  so a redeploy or a dropped Redis connection mid-encounter leaves those visits
  unwatched until they end, and the provider sees a quiet visit. The consumer
  logs at WARNING when it happens, which is the honest version of the problem
  rather than a fix. Rebuilding the watch set would mean querying `encounters`
  for active rows on reconnect — real work with its own failure modes, deferred
  to the task that makes this path produce actual queries. TASK-030 makes the
  same trade for its transcript buffer, deliberately.
- The two consumers of the transcript — `track-a-clinical` for SOAP generation
  and `track-b-rag` for keyword scanning — are independent. They subscribe to
  the same channel and never see each other.

## References

- `services/track-a-clinical/src/track_a_clinical/api/sessions.py`
- `services/track-b-rag/src/track_b_rag/transcript_consumer.py`
- `CLAUDE.md` -> Redis Key Naming — Canonical List
