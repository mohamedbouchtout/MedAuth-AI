# ADR-0031: The CPT resolver refuses rather than guesses

**Status:** Accepted · **Task:** TASK-024

## Context

Keyword detection yields `"MRI"`. `/policies/query` needs a CPT code, and that
is a different question: a payer publishes criteria **per code**, and "MRI"
spans dozens of them by body part and by contrast.

A resolver that picks the most likely code when the phrase is ambiguous would
raise more nudges. It would also be catastrophic, because of the cache key.
`rag:{payer}:{plan_type}:{state}:{cpt_code}` means a guessed code does not merely
give one encounter a poor answer — it files a **real, cacheable payer-policy
answer under a key standing for a different procedure**, and every later
encounter landing on that key is served it. The failure is silent and it crosses
patients.

## Decision

`resolve_procedure_code()` returns `NoProcedureCode` wherever the spoken phrase
does not determine a code, and the caller does not query at all.

Not querying costs one missed nudge for one order. A wrong code costs a wrong
answer for every later encounter that lands on the key.

**Four distinct reasons are reported**, because they are not equally fixable and
an operator reading a log line should be able to tell them apart:

| Reason | Meaning |
|---|---|
| `no_cpt_exists` | The keyword names a class of therapy or an administrative act, not a coded procedure. "biologic" is a drug whose HCPCS J-code is specific to the agent; "referral" has no CPT code at all. Permanent. |
| `axis_not_spoken` | A real coded procedure whose code turns on something nobody says out loud — an X-ray's number of views, an arthroscopy's intra-operative findings. Effectively permanent, for a different reason. |
| `qualifier_not_stated` | The module knows the axis and the excerpt named none. Fixable per encounter: the clinician said "let's get an MRI" and not which MRI. |
| `qualifier_unmapped` | A qualifier was recognised and the table has no code for the pairing. **This is the one that means extend the table**, and it is why the four are separated at all. |

**An entry exists when the spoken phrase pins the code down to the level at
which payers publish criteria.** Where an unstated axis would change the
authorization answer, there is no entry. Where an entry does fix an unstated
axis — an MRI order means "without contrast" unless someone escalates it — the
assumption is named in `ProcedureCode.assumes` rather than left implicit, so a
reviewer can see what was decided on their behalf.

**The qualifier axis is not always a body site.** MRI and CT turn on anatomy, a
stress test on modality, an arthroscopy on the planned intervention, a joint
injection on joint size. Reading "which axis selects this code" as "which body
part" is what made an earlier draft exclude arthroscopy and injection outright —
the axis *was* spoken, it just was not the one being looked for. Both are mapped.

## Consequences

- **X-ray and biopsy remain unmapped, and that is a decision.** An X-ray's code
  is selected by the number of views, chosen by the technologist at the machine,
  and plain radiography is essentially never prior-authorization gated — a query
  would spend a Qdrant search and a Sonnet call to report a miss indistinguishable
  from a corpus we do not hold. "biopsy" spans body systems whose code families
  have nothing in common: the gated ones (breast) split by imaging guidance and
  the ones our target specialties order most (skin) split by technique, neither
  of which is spoken.
- **The codes in this table are not clinically verified, and CPT is
  AMA-licensed material.** The descriptors are short paraphrases, not the AMA's
  long descriptors. Both the codes and the pairings need a certified coder's
  review before anything they produce reaches a provider. Nothing can reach one
  yet — `resolve_query_parameters()` cannot build a query at all until TASK-052b
  lands — and that review is a prerequisite on TASK-052b for exactly this reason.
- **Extension is by adding rows, not by making the matcher cleverer** — the same
  shape `packages/payer-vocab` uses. It still needs a deploy, which is a product
  problem; TASK-024b tracks moving the table behind a loader so it can be data.
- It is a module in this service rather than a package because track-b-rag is
  its only consumer today. It moves to `packages/procedure-codes` when
  `prior-auth` needs it for bundle assembly (TASK-060) — the same trigger that
  extracted `api-envelope`. Unlike a payer slug, a CPT code is an external
  identifier this repo does not mint, so nothing stored depends on where the
  table lives and the move costs an import.

## References

- `services/track-b-rag/src/track_b_rag/procedure_codes.py`
