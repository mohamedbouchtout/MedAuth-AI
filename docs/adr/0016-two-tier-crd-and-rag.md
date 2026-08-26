# ADR-0016: Two-tier lookup — CRD decides `requires_auth`, RAG supplies the criteria

**Status:** Accepted · **Task:** TASK-015

## Context

CMS-0057-F requires Medicare Advantage, Medicaid managed care, CHIP and ACA
marketplace payers to expose a standardised CDS Hooks API for coverage
requirements by 2027-01-01. Where a payer offers one, its answer to *does this
need prior authorization* is authoritative in a way that reasoning over its
published PDFs is not.

Earlier drafts concluded that CRD would let us "skip the RAG path entirely" for
covered payers. That was written before anyone had run a CRD server.

**What CRD actually carries was established empirically.** The CRD Reference
Implementation was run locally and queried for several codes, and the IG's own
`ext-coverage-information` StructureDefinition was then read to check whether the
output was an RI shortcut or the shape of the standard. It is the standard: the
extension's slices are `covered`, `pa-needed`, `doc-needed`, `doc-purpose`,
`info-needed`, `questionnaire`, `reason`, `detail`, `billingCode` and assorted
dates and trace identifiers. **There is no slice carrying criterion text.** CRD
answers *whether* authorization and documentation are needed and delegates *what
must be documented* to a DTR Questionnaire.

A CRD-only answer would therefore hand Stage 2 an empty criteria list and produce
a nudge that cannot say what is missing — which is most of the product.

## Decision

For a payer covered by the mandate, `/policies/query` runs **both** tiers
**concurrently**:

- **CRD decides `requires_auth`**, and its answer overrides the one reasoned out
  of policy text. It is the one field the payer has stated directly rather than
  published in prose for us to interpret.
- **The RAG path supplies `auth_criteria` and the step therapy fields**, exactly
  as it does for everyone else.

Commercial employer-sponsored plans — the bulk of what private practices see —
are not covered by the mandate and take the RAG path alone. Both arrangements
return the same response shape; callers never branch on which tier answered.

The tiers run concurrently because they need nothing from each other and a nudge
is read mid-encounter.

**Two dialects are read, both real.** A spec-conformant payer states the
determination in the `pa-needed` slice. The Reference Implementation never emits
that slice — it emits a `coverageInfo` slice that is not in the IG's slice list
at all, and states the determination in the card's *type*
(`source.topic.code == "prior-auth"`). A mapping written against the IG alone
finds nothing in RI output; one written against RI output alone misses a real
payer. The conformant signal is checked first, with the card type as fallback.

**Silence is never a negative determination.** An empty card list, a
documentation-only card, and the "unable to process" card a payer returns when
its rule needs more than we sent all mean *no determination*, and the RAG path
then answers alone.

## Consequences

- The tier can only add an answer, never remove one. An unconfigured
  `CRD_BASE_URL`, an unsupported payer, a timeout, or a decision-free response
  all yield `None` and the service behaves exactly as it did before TASK-015.
- Where the RAG tier fell back but CRD answered, the result is a real answer
  (`source == "crd"`) with an empty criteria list: the payer has told us whether
  authorization is required, which is more than the fallback claims to know.
- Provenance is reported explicitly — `cache`, `rag`, `fallback`, `crd`,
  `crd+cache`, `crd+rag` — because with CRD results deliberately uncached,
  "the CRD tier answered" can no longer be inferred from cache state.
- The timeout is 4.0s, measured against the RI: ~0.5s steady state and ~3.0s on
  the first request while it compiles its CQL rule libraries. It is not a knob
  for making a slow payer work — a payer that cannot answer in four seconds
  should fall through to RAG, which is what a timeout does.
- **DTR is deferred.** Following the questionnaire canonical and turning its
  items into criteria needs a SMART on FHIR app surface that does not exist
  before Phase 5, and its items are largely administrative form fields (name,
  NPI, signature) that would have the Stage 2 matcher report a clinician's note
  as missing "Signature".
- `CRD_SUPPORTED_PAYERS` is a literal frozenset of two slugs, not a config file.
  CHIP and ACA marketplace have no slug yet because no observed
  `Coverage.payor.display` has produced one, and speculative slugs are exactly
  the mistake ADR-0022 rules out.

## References

- `services/track-b-rag/src/track_b_rag/crd.py`, `policy_rules.py`
- `services/track-b-rag/tests/fixtures/crd/`, `tests/integration/test_crd_live.py`
- `CLAUDE.md` -> Policy lookup is two-tier
