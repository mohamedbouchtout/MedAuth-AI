"""Stage 2 — this patient's documentation, measured against the payer's criteria.

Stage 1 says what the payer requires; this module says what *this encounter*
has not yet documented, and how likely a claim is to be denied as a result. Its
three outputs — ``missing_criteria``, ``denial_risk`` and ``nudge_message`` —
describe one patient and are never cached. Caching them under the payer-scoped
key would hand one patient the gaps computed for another; TASK-012 and
CLAUDE.md's cache note both record that as the reason the two stages are split.

**Deterministic, and deliberately not a second model call.** Every input it
needs is already in memory once Stage 1 has answered, and the comparison runs on
every request — including the cached ones, which is most of them. Doing it in
Python keeps the cache's cost saving intact, keeps the per-nudge latency inside
what a live encounter tolerates, and makes the output reproducible in a test
rather than something to be asserted loosely. It is also the only reason
TASK-012's "no Bedrock call on a cache hit" test can be written as an equality
rather than a hope.

The matching itself is an explicit term-overlap heuristic, not natural language
understanding, and it is the part of this module most likely to be replaced
later. Two properties matter more than its precision: it is deterministic, and
it errs toward reporting a criterion as missing. A criterion wrongly listed as
missing costs a provider a glance at a nudge; a criterion wrongly treated as
satisfied is a silent gap in a prior authorization, which is the failure
direction TASK-012 rules out.

No PHI leaves this module. The clinical context is read here and referenced
nowhere in the output: ``missing_criteria`` echoes the payer's own criteria
text, and ``nudge_message`` is built from those criteria and the procedure name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from track_b_rag.policy_rules import PolicyRules

DenialRisk = Literal["low", "medium", "high"]

#: Share of a criterion's content terms that must appear in the clinical context
#: for it to count as documented. Two thirds of a three-term criterion, three of
#: five. Tuned to be forgiving about phrasing and unforgiving about absence.
CRITERION_COVERAGE_THRESHOLD: Final = 0.6

#: How many criteria a nudge names before summarising the rest. A nudge is read
#: mid-encounter, on a phone or a sidebar, by someone talking to a patient.
NUDGE_CRITERIA_LIMIT: Final = 3

#: Function words plus the boilerplate every payer criterion is written in.
#: Removing them leaves the clinical content, which is what a note can actually
#: be matched against — "documentation of failed conservative therapy" matches
#: on *failed conservative therapy*, not on *documentation of*.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "and",
        "any",
        "are",
        "authorization",
        "been",
        "criteria",
        "documentation",
        "documented",
        "each",
        "evidence",
        "following",
        "for",
        "has",
        "have",
        "including",
        "least",
        "member",
        "must",
        "not",
        "one",
        "patient",
        "per",
        "prior",
        "provider",
        "record",
        "records",
        "request",
        "requested",
        "required",
        "requires",
        "shall",
        "should",
        "such",
        "that",
        "the",
        "their",
        "there",
        "this",
        "used",
        "was",
        "were",
        "which",
        "will",
        "with",
        "within",
    }
)

_TOKEN_PATTERN: Final = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class DocumentationAssessment:
    """What this encounter still needs, and how risky the gap is."""

    missing_criteria: list[str]
    denial_risk: DenialRisk
    nudge_message: str


def assess(
    *,
    rules: PolicyRules,
    clinical_context: Mapping[str, Any],
    procedure: str,
) -> DocumentationAssessment:
    """Compare the payer's criteria against this encounter's documentation.

    Args:
        rules: Stage 1's answer — the payer's rules for this procedure.
        clinical_context: What is documented for this patient so far. Read here
            and never echoed into the result.
        procedure: The procedure as the clinician described it, used only to
            address the provider in the nudge.

    Returns:
        The criteria not yet evidenced, the resulting denial risk, and the
        message to put in front of the provider.
    """
    context_terms = context_vocabulary(clinical_context)
    missing = [
        criterion
        for criterion in rules.auth_criteria
        if not is_documented(criterion, context_terms)
    ]
    risk = denial_risk(rules=rules, missing=len(missing), total=len(rules.auth_criteria))
    return DocumentationAssessment(
        missing_criteria=missing,
        denial_risk=risk,
        nudge_message=nudge_message(rules=rules, missing=missing, procedure=procedure),
    )


def context_vocabulary(clinical_context: Mapping[str, Any]) -> frozenset[str]:
    """Return every content term the clinical context mentions.

    Keys are flattened alongside values: a structured context expresses as much
    in ``{"conservative_therapy_failed": true}`` as a narrative one does in a
    sentence, and reading only the values would miss it entirely.
    """
    return frozenset(_terms(" ".join(_flatten(clinical_context))))


def is_documented(criterion: str, context_terms: frozenset[str]) -> bool:
    """Return whether the context covers enough of a criterion to call it met.

    A criterion with no content terms of its own — one written entirely in
    boilerplate — is reported as *not* documented, because there is nothing to
    check it against and an unverifiable criterion is one for a human to read.
    """
    terms = _terms(criterion)
    if not terms:
        return False
    covered = sum(1 for term in terms if term in context_terms)
    return covered / len(terms) >= CRITERION_COVERAGE_THRESHOLD


def denial_risk(*, rules: PolicyRules, missing: int, total: int) -> DenialRisk:
    """Return the denial risk implied by how much of the criteria list is unmet.

    Nothing required means nothing to deny. Where authorization is required, the
    risk tracks the share of criteria still undocumented, with one special case:
    a payer that requires authorization but published no criteria we could find
    is a *medium* risk rather than a low one, because "no criteria" here means
    "not known", not "none".

    A step therapy requirement raises the floor to medium. Step therapy is
    checked from the payer's side as a prerequisite before the request is even
    considered, so a plan that has one is never a low-risk submission — but it
    does not by itself make an otherwise well-documented request a high-risk
    one, which is why it lifts the floor rather than escalating every level.
    """
    if not rules.requires_auth:
        return _with_step_therapy_floor("low", rules)
    if total == 0:
        return _with_step_therapy_floor("medium", rules)
    if missing == 0:
        return _with_step_therapy_floor("low", rules)
    if missing / total <= 0.5:
        return _with_step_therapy_floor("medium", rules)
    return "high"


def nudge_message(*, rules: PolicyRules, missing: Sequence[str], procedure: str) -> str:
    """Return the one-or-two sentence message a provider sees mid-encounter.

    Built from the payer's criteria and the procedure name only. Nothing from
    the clinical context reaches it — the provider already knows what is in
    their own note, and a nudge is rendered in a browser and relayed over a
    WebSocket, which is not somewhere to put clinical detail that need not be
    there.
    """
    if not rules.requires_auth:
        head = f"No prior authorization required for {procedure}."
    elif not rules.auth_criteria:
        head = (
            f"Prior authorization required for {procedure}, but no published criteria "
            "were found for this plan — confirm the requirements manually."
        )
    elif not missing:
        head = (
            f"Prior authorization required for {procedure}. Every documented criterion "
            "appears to be met."
        )
    else:
        head = (
            f"Prior authorization required for {procedure}. "
            f"Still undocumented: {_criteria_phrase(missing)}."
        )

    if not rules.step_therapy_required:
        return head
    detail = rules.step_therapy_details or "a first-line therapy must be tried first"
    return f"{head} This plan also requires step therapy: {detail}"


def _with_step_therapy_floor(risk: DenialRisk, rules: PolicyRules) -> DenialRisk:
    """Raise `risk` to at least medium when the plan requires step therapy."""
    if rules.step_therapy_required and risk == "low":
        return "medium"
    return risk


def _criteria_phrase(missing: Sequence[str]) -> str:
    """Render the missing criteria as a readable list, capped for a live nudge."""
    shown = [criterion.rstrip(".") for criterion in missing[:NUDGE_CRITERIA_LIMIT]]
    phrase = "; ".join(shown)
    remaining = len(missing) - len(shown)
    if remaining > 0:
        phrase += f"; and {remaining} more"
    return phrase


def _flatten(value: Any) -> list[str]:
    """Return every string, key and scalar inside a nested structure, in order."""
    if isinstance(value, Mapping):
        parts: list[str] = []
        for key, item in value.items():
            parts.append(str(key))
            parts.extend(_flatten(item))
        return parts
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [part for item in value for part in _flatten(item)]
    if value is None:
        return []
    return [str(value)]


def _terms(text: str) -> frozenset[str]:
    """Return the content terms in `text`.

    Words of three characters or more survive, minus the stopword list. Numbers
    survive at any length, because a criterion's numbers are usually the whole
    of it — "six weeks" and "twelve weeks" of conservative therapy are different
    requirements, and dropping the digits would make them the same string.
    """
    tokens = _TOKEN_PATTERN.findall(text.lower())
    return frozenset(
        token
        for token in tokens
        if token not in _STOPWORDS and (len(token) >= 3 or token.isdigit())
    )
