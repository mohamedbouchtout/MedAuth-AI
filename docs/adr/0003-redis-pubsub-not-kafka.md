# ADR-0003: Redis pub/sub is the message bus until >20 providers

**Status:** Accepted · **Task:** TASK-001

## Context

The real-time path fans one audio stream out to two independent consumers, and
fans nudges back to connected clients. That is a message bus. Kafka is the
default answer for an event-driven clinical platform, and it brings durable
partitioned logs, consumer groups, replay, and an operational burden — a
cluster, a schema registry, and someone who understands both — that a
pre-first-customer system cannot absorb.

Nothing in the current pipeline needs durability. A transcript segment is useful
for the seconds during which the encounter is still happening; a nudge that
arrives after the patient has left has failed regardless of whether it was
replayed. The consumers are online or the message is worthless.

## Decision

Redis pub/sub carries every inter-service message until the platform passes
**20 providers**. Postgres already runs for the domain schema and Redis already
runs for the RAG cache, so the bus adds no new infrastructure at all.

Service interfaces are written so that swapping the transport later is a
configuration change: publishers call a module-level publish function and
consumers subscribe to a channel name built from a template. Nothing above those
functions knows what is underneath them.

## Consequences

- **No durability, and that is understood.** A consumer that is not subscribed
  when a message is published never sees it. The transcript consumer logs at
  WARNING when a restart loses the sessions it was watching, which is the honest
  version of the problem rather than a fix.
- Channel names are fixed by `CLAUDE.md`'s canonical Redis key list, so a task
  cannot invent a variant. A new pattern is added to that list in the same PR.
- At >20 providers the trade reverses — durable replay and consumer groups start
  to matter — and this ADR gets superseded rather than quietly stretched.

## References

- `CLAUDE.md` -> Redis Key Naming — Canonical List
- `services/audio-ingestion/src/publisher.py`
- `services/track-b-rag/src/track_b_rag/transcript_consumer.py`
