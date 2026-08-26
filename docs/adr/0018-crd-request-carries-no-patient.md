# ADR-0018: The CRD request carries no patient

**Status:** Accepted · **Task:** TASK-015; closed by TASK-059

## Context

CDS Hooks CRD is specified as a *patient-specific* coverage check. The request
context names a subject, and a payer rule may legitimately key on age, sex or
existing coverage.

Stage 1 of the policy query holds no patient **by construction** — that is the
whole point of ADR-0014. So there is nothing to put in the subject.

## Decision

The CRD request is built from payer, plan type, state and procedure code only,
with a placeholder subject (`policy-query`) that is not a patient identifier and
is not derived from one. It exists because CDS Hooks requires the context to
name a subject.

A payer rule that needs demographics simply cannot answer us. It returns an
"unable to process" card or an error, which this module reports as *no
determination*, and the RAG path answers instead — exactly as it does for a
commercial plan.

**Fabricating a patient to make such a rule respond is ruled out.** It would
produce a confident determination about someone who does not exist, and that
determination would be shown to a provider as the payer's own answer.

## Consequences

- Some CRD-covered payers will decline to answer, and the tier degrades to RAG
  for them. That is the correct behaviour, not a limitation to work around.
- **This is a real gap, tracked as TASK-059**, gated on TASK-052 supplying real
  `Patient` and `Coverage` resources from the EHR.
- Note what changes when TASK-059 lands, because it is more than passing an id:
  a patient-carrying CRD request is a **PHI disclosure to a third party**. TLS
  stops being a deployment convention and becomes a requirement; the endpoint
  has to be verified per payer rather than read from one `CRD_BASE_URL`; and the
  disclosure needs its own audit row. None of that applies to the patient-free
  request built today, which is part of why the split is worth keeping explicit.

## References

- `services/track-b-rag/src/track_b_rag/crd.py` (`_PLACEHOLDER_ID`)
- `CLAUDE.md` -> The CRD request carries no patient
