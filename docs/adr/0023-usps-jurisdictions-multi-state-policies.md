# ADR-0023: Jurisdictions normalise to USPS codes, and a multi-state policy is one document

**Status:** Accepted · **Task:** TASK-013, TASK-016

## Context

This is ADR-0022's problem one column over, and it bites for the same reason: a
policy is stored under whatever code its source used, a query arrives with
whatever code the encounter carries, and the two are compared by exact equality.

A FHIR `Coverage` says `NY`. CMS's Medicare Coverage Database says `DN`, `QN` or
`UN` — New York downstate, Queens and upstate, three Medicare Administrative
Contractor jurisdictions inside one state. Its `state_lookup` table also carries
`NF`/`SF` for northern and southern California, `EM`/`WM` for Missouri, and a
four-character `CNMI` that does not fit the `CHAR(2)` column it would be written
to. Neither vocabulary is wrong; nothing was translating between them.

Separately, an LCD is issued per MAC jurisdiction and applies across every state
that jurisdiction covers — a **median of 12 states** across the 949 current
LCDs, and 48 for the widest.

## Decision

**Normalise to the two-character USPS code of the parent state at ingestion
time**, in `packages/payer-vocab`: `CNMI` -> `MP`, `DN`/`QN`/`UN` -> `NY`,
`NF`/`SF` -> `CA`, `EM`/`WM` -> `MO`. Everything downstream then compares USPS
codes against USPS codes.

**A multi-state policy is one document with a list of states**, not one copy per
state. The Postgres row carries a `jurisdiction_states` text array; the Qdrant
payload carries `state: ["MA", "ME", "NY"]`.

This required **no change to the retrieval filter**. Qdrant's `MatchValue`
matches any element of a list-valued payload field — verified against the running
Qdrant using the exact filter in `policy_query_filter`, with and without the
keyword payload index. The `IsNullCondition` that lets national policies match
every state keeps working alongside it.

## Consequences

- Copying a policy per state would duplicate identical text a median of 12 times
  in Qdrant: 12x the embedding cost, and near-duplicate chunks crowding each
  other out of `TOP_K = 8`.
- The state half of the retrieval filter is not a plain equality check. Expressed
  as Qdrant's `should` alongside a `must`, it reads: *this payer, and either this
  state or no state at all*. A policy ingested with no state applies nationally —
  CMS national coverage determinations are the obvious case — and an equality
  filter would hide every one of them from a query that named a state.
- A code outside the recognised USPS set is a bug at the caller, not a
  jurisdiction we have not met yet, and is treated as one.

## References

- `packages/payer-vocab/src/payer_vocab/states.py`
- `services/track-b-rag/src/track_b_rag/retrieval.py` (`policy_query_filter`)
- `services/track-a-clinical/migrations/versions/0002_policy_jurisdiction_states.py`
