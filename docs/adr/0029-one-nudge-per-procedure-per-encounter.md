# ADR-0029: One nudge per procedure per encounter, claimed atomically in Redis

**Status:** Accepted · **Task:** TASK-021, TASK-024

## Context

A clinician names a procedure more than once in a visit — proposing it,
confirming it, dictating it back. Every stabilized segment carrying the word
reaches track-b-rag, and without a guard each one costs a policy query and
raises another nudge for an order the provider has already been told about.

The first mention is the useful one. The rest are noise that trains people to
dismiss the banner without reading it, which destroys the product.

## Decision

A per-session Redis set, `procedure_seen:{session_id}` with a 4-hour TTL, holds
the procedure keys already queried during the encounter. Members are
`cpt:{code}` where a CPT code resolves and `keyword:{keyword}` where none does.

Four properties, each load-bearing:

- **The claim is atomic.** `SADD` reports whether the member was actually added,
  in one round trip, so two segments arriving close together cannot both read
  "not seen yet" and both fire. A read-then-write pair would have exactly that
  race, and the window is real — Transcribe emits stabilized results in bursts.
- **The state is in Redis, not in the process.** Every instance answers for every
  session, so an in-memory set would suppress a repeat only when it happened to
  land on the same pod: a guard that works on one laptop and stops working the
  day a second replica exists.
- **One key per session, not one per procedure**, so ending a session clears the
  encounter's state with a single `DEL`. The TTL is only a safety net for a
  session that never ends — a client that crashed mid-visit — not the mechanism.
- **An unreachable Redis fails open.** A failed claim is treated as a first
  mention, so the query fires and the provider may see one duplicate nudge.
  Failing closed would suppress a nudge for a procedure nobody has been warned
  about, which is the one direction this pipeline must never fail in.

**Keying on the CPT code where one resolves is what makes two keywords naming
one procedure share a claim**: a knee MRI and a hip MRI are both `73721` and are
one order.

A claim is released only when the query failed in a way that could succeed next
time, so a transient failure does not permanently suppress the nudge.

## Consequences

- This is the second of two duplicate-suppression mechanisms; ADR-0027 removes
  duplicates within one utterance and this removes them across an encounter.
- A genuinely repeated *order* of the same procedure raises one nudge. That is
  the intended behaviour.

## References

- `services/track-b-rag/src/track_b_rag/dedup.py`
- `services/track-b-rag/src/track_b_rag/policy_dispatch.py` (`procedure_key`)
