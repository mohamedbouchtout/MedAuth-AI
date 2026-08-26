# ADR-0022: Payer identity is a canonical slug from one vocabulary package

**Status:** Accepted · **Task:** TASK-016

## Context

`payer` is matched by exact string equality in two places: Qdrant's retrieval
filter and the Redis cache key `rag:{payer}:{plan_type}:{state}:{cpt_code}`.
Nothing normalised it on either side — ingestion stored whatever string the
uploader sent, and the query endpoint filtered on whatever string the caller
sent. `state` and `cpt_code` were at least uppercased; `payer` was not touched
at all.

At query time that string originates in a FHIR `Coverage` resource's free-text
payer display: "Medicare Part B", "AETNA", "Aetna Better Health of MA". None of
those equals the "CMS" or "Aetna" an ingest wrote.

**The failure was silent.** Retrieval returned zero chunks, the RAG path
reported it found no policy, and that was indistinguishable from a payer we
genuinely hold no policy for.

## Decision

`payer` is a **canonical slug** everywhere it is stored, matched or keyed, and
never a display name. Both sides call the same functions from
`packages/payer-vocab`:

```python
normalize_payer(raw: str) -> str   # "Medicare Part B" -> "cms-medicare"
is_known_payer(slug: str) -> bool
```

Two layers, in this order:

1. **Deterministic slugging** — casefold, strip legal suffixes and punctuation,
   collapse whitespace, hyphenate. This guarantees two spellings of one name
   cannot become two payers. It needs no curation and never goes stale.
2. **An alias table**, consulted on the slug, for what slugging cannot reach.
   No string manipulation turns "Medicare Part B" into "CMS"; only knowing they
   name the same payer does. That is curated data — extend the table when a new
   payer appears, rather than making the slug function cleverer.

Four rules govern it:

- **An unknown payer still queries, but says so.** It gets a slug and runs. The
  query path logs at WARNING when `is_known_payer()` is false, naming both
  spellings, so "the name did not line up" stays distinguishable from "no policy
  found". That distinction is the whole reason the package exists.
- **Display names are not discarded.** The payer's own spelling is kept in the
  Postgres row for humans. Slugs are for matching, not for display.
- **A payer family is not one payer.** The BCBS Association licenses 33
  independent companies that each publish their own criteria. Anthem-branded
  names resolve to `anthem-bcbs`, a licensee we hold policies for gets its own
  slug (`bcbs-ma` first, Massachusetts being the pilot geography), and an
  unqualified "Blue Cross" lands in a generic `blue-cross-blue-shield` bucket.
  Collapsing them would let one licensee's policy answer a query about another —
  a wrong answer served silently, strictly worse than an empty retrieval.
- **Extend the alias table from observed data, not plausible spellings.** The
  rows for `Coventry Healthcare`, `Cigna Health`, `Medi-Cal` and
  `Humana Medicare Advantage` exist because those exact strings appear in
  `Coverage.payor.display` on the Oracle Health sandbox, the public HAPI R4
  server, and Synthea's `insurance_companies.csv`. Names that describe no
  carrier at all (`SELF PAY`, `Government`, `Dual Eligible`) are deliberately
  left unmapped: giving them a slug would manufacture a payer identity the
  source never asserted.

## Consequences

- It is a **package**, not a module inside track-b-rag, because the consumers
  span services: `/policies/ingest`, `/policies/query`, the scraper, the seed
  script, and `fhir-integration` when it turns a `Coverage` into a query.
- Doing this after TASK-014 seeded a corpus would have meant re-ingesting that
  corpus. Doing it while the index was dev-only cost a re-run.
- Seed and ingest under the publishing licensee's slug, never a generic one.

## References

- `packages/payer-vocab/src/payer_vocab/payers.py`
- `services/track-b-rag/src/track_b_rag/api/query.py` (`_resolve_payer`)
- `CLAUDE.md` -> Payer and jurisdiction identity
