# ADR-0030: Procedure detection is a keyword list, not a model

**Status:** Accepted · **Task:** TASK-021

## Context

Something has to notice that "let's get an MRI of that knee" is a procedure
order. The accurate way is a model — an LLM or a fine-tuned classifier — reading
each transcript segment.

This runs on **every stabilized segment of every live encounter**. A model call
per segment would cost more than the policy query it guards, and would add its
own latency to a budget measured in seconds.

## Decision

A fixed, literal keyword list, compiled to regular expressions: MRI, CT scan,
X-ray, biopsy, injection, arthroscopy, echocardiogram, stress test, biologic,
chemotherapy, and a referral to a named specialist. Around a match, two
sentences of context are extracted as the excerpt.

Two properties matter more than accuracy:

- **Speed and predictability.** No network call, no variance.
- **Reviewability.** A clinician can read this list and say exactly what will
  and will not raise a nudge. That is not true of a model.

**A match is a candidate, never a determination.** Nothing here decides a
procedure was ordered. "We could do an MRI but let's wait" matches, and should:
the downstream policy query establishes whether authorization is required at
all, and a nudge appears only when documentation is actually missing. The module
biases toward detecting, for the same reason the rest of the service biases
toward flagging (ADR-0015) — the failure that matters is a missed order, not an
extra check.

## Consequences

- **The cost is recall.** A procedure named some other way is missed entirely.
  That is an accepted trade for v1, not an oversight.
- Extending the vocabulary is a code change and a deploy. A practice cannot add
  its own terms, which is a product limitation tracked alongside the CPT table's
  (see ADR-0031 and TASK-024b).
- **Every string handled here is PHI.** The text is what was said in an exam
  room; it is matched, sliced and handed on, and never logged. Log lines about a
  detection name the canonical keyword — "MRI" — and never the excerpt.

## References

- `services/track-b-rag/src/track_b_rag/keywords.py`
