# ADR-0020: Ingestion writes Qdrant before Postgres

**Status:** Accepted · **Task:** TASK-011

## Context

Ingesting a policy writes two stores: the chunk vectors go to Qdrant, and one
row describing the document — including its `content_hash` — goes to Postgres.
They share no transaction, so a crash between them leaves the two disagreeing.
The write order decides *which way* that partial failure fails.

Dedup is keyed on `(policy_id, content_hash)`: no row means index and insert
(`created`); a row whose digest matches means do nothing (`unchanged`); a row
whose digest differs means re-index and update (`updated`).

## Decision

**Qdrant first, Postgres second.**

## Consequences

- A crash in between leaves the stored `content_hash` **stale**. The next scrape
  computes a digest that does not match, re-ingests, and the system repairs
  itself. Wasteful, self-correcting, and visible in the run summary.
- Reversed, the row would claim to be current while its vectors were missing or
  half-replaced, and **nothing would ever retry**. Retrieval would quietly
  return less than it should, or return chunks of a superseded policy. There is
  no signal that this has happened.
- The order is recorded in `TASKS.md` as well, so it does not get "simplified"
  later by someone who reads the two writes as interchangeable.
- Ingestion writes **no audit row**: policy documents are public payer
  publications with no patient linkage (ADR-0006). The INFO log is the
  operational record instead.

## References

- `services/track-b-rag/src/track_b_rag/ingestion.py`
