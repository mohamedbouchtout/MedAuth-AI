# ADR-0038: An env-gated live test is paired with a scheduled run

**Status:** Accepted · **Task:** TASK-013, TASK-015

## Context

Some tests depend on a live external source: the CMS Medicare Coverage Database,
the Da Vinci CRD Reference Implementation, and later the payer and EHR sandboxes.

Those do not belong in the per-PR suite. An unrelated pull request should not go
red because a government site is down or a vendor sandbox is being rebuilt. The
standard answer is an environment-variable gate, defaulted off.

**A gate on its own is not a deferral — it is a deletion.** Nothing ever
executes the test, and drift in the external source surfaces whenever someone
happens to flip the flag, which is to say at random.

## Decision

Every env-gated live test is paired with `.github/workflows/nightly-live-checks.yml`,
which runs on `schedule:` with the gate set, plus `workflow_dispatch` for running
it by hand.

Four rules for anything added there:

- **The gate defaults to off**, so `pytest` on a laptop and in CI behave the same.
- **The job names the external dependency in its own name**, so a red nightly
  says *which* upstream moved without anyone opening the log.
- **A failure is a real signal about the outside world, not a flake.** Fix the
  code or the fixtures; do not relax the assertion.
- **Never put a test here to escape the per-PR suite.** Only genuine external
  dependencies qualify. Anything that can run against a fixture stays on in CI.

## Consequences

- A scheduled failure naming the source that changed is the honest version of
  "don't loosen the test to mask drift".
- This is the mechanism behind a broader rule: any layer that parses or maps an
  external source's output is validated against **real output from that source**,
  not only hand-written fixtures. `tests/integration/test_cms_live.py` and
  `tests/integration/test_crd_live.py` are both instances, and both are backed
  by committed fixtures captured from real responses so the unit suite has
  something faithful to run against offline.

## References

- `.github/workflows/nightly-live-checks.yml`
- `services/policy-scraper/tests/integration/test_cms_live.py`
- `services/track-b-rag/tests/integration/test_crd_live.py`, `tests/fixtures/crd/`
