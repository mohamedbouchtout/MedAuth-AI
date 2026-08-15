---
name: Bug report
about: Something in MedAuth AI behaves incorrectly
title: "bug(<service>): <short description>"
labels: ["bug", "triage"]
assignees: ""
---

<!--
⚠️ NEVER paste PHI into an issue. GitHub is not a HIPAA-eligible store.
No patient names, MRNs, dates of birth, addresses, real transcripts, or real
audio. Redact to synthetic values, or reference a Synthea patient ID instead.
-->

## What happened

<!-- Observed behavior. -->

## What should have happened

<!-- Expected behavior. -->

## Reproduction steps

1.
2.
3.

## Affected area

- Service / app: <!-- e.g. services/track-b-rag, apps/web -->
- Related task: <!-- e.g. TASK-012, or "unknown" -->
- Environment: <!-- local / dev / staging -->

## Logs or error output

<!-- Scrub PHI before pasting. Include the request id and session id if you have them. -->

```
```

## Environment details

- OS:
- Python / Node version:
- Commit SHA or branch:
- EHR vendor, if EHR-related: <!-- athena / ecw / modmed / cerner / epic / local HAPI -->

## Impact

- [ ] Blocks a clinical encounter in progress
- [ ] Produces an incorrect clinical note, code, or nudge
- [ ] Possible PHI exposure — **also notify the security owner directly, do not wait on triage**
- [ ] Degraded but recoverable
- [ ] Cosmetic

## Additional context

<!-- Screenshots (redacted), links, anything else useful. -->
