"""Procedure-order keyword detection over one transcript segment.

This is the trigger for the whole Track B path: a clinician says "let's get an
MRI of that knee", and the policy query that becomes a nudge starts here. The
keyword list is fixed by TASK-021 and is deliberately small and literal — MRI,
CT scan, X-ray, biopsy, injection, arthroscopy, echocardiogram, stress test,
biologic, chemotherapy, and a referral to a named specialist.

**Why a keyword list rather than a model.** This runs on every stabilized
segment of a live encounter, so it has to be fast and predictable; a model call
per segment would cost more than the policy query it guards. It also has to be
reviewable: a clinician can read this list and say what will and will not raise
a nudge. The cost is recall — a procedure named some other way is missed
entirely — and that is an accepted trade for v1, not an oversight.

**A match is a candidate, never a determination.** Nothing here decides that a
procedure was ordered. "We could do an MRI but let's wait" matches, and should:
the downstream policy query establishes whether authorization is required at
all, and a nudge appears only when documentation is actually missing. This
module biases toward detecting, for the same reason the rest of the service
biases toward flagging — the failure direction that matters is a missed order,
not an extra check.

**Every string handled here is PHI.** ``text`` is what was said in an exam room.
It is matched, sliced and handed to the caller, and it is never logged. Log
lines about a detection name the canonical keyword — "MRI" — and never the
excerpt around it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Matched with :data:`re.IGNORECASE`, so each pattern is written in lower case.
#: Word boundaries on both ends throughout: "CT" must not fire inside "CTA", and
#: "biologic" must not fire inside a longer token.
_KEYWORD_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("MRI", r"\bmris?\b"),
    ("CT scan", r"\bct\s+scans?\b"),
    # "x-ray", "x ray" and "xray" all reach us from Transcribe depending on how
    # the word was said; they are one keyword, not three.
    ("X-ray", r"\bx[-\s]?rays?\b"),
    ("biopsy", r"\bbiops(?:y|ies)\b"),
    ("injection", r"\binjections?\b"),
    ("arthroscopy", r"\barthroscop(?:y|ies)\b"),
    ("echocardiogram", r"\bechocardiograms?\b"),
    ("stress test", r"\bstress\s+tests?\b"),
    ("biologic", r"\bbiologics?\b"),
    ("chemotherapy", r"\bchemotherapy\b"),
)

#: "referral to [specialist]" is a pattern rather than a literal, so it is
#: handled separately: the specialist is part of what was ordered and has to
#: reach the policy query, which is why the label this produces is
#: "referral to orthopedics" and not a bare "referral".
_REFERRAL_KEYWORD: Final = "referral"
_REFERRAL_PATTERN: Final = re.compile(
    r"\breferral\s+to\s+(?:an?\s+|the\s+)?(?P<specialist>[a-z][a-z-]*(?:\s+[a-z][a-z-]*)?)",
    re.IGNORECASE,
)

_COMPILED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (keyword, re.compile(pattern, re.IGNORECASE)) for keyword, pattern in _KEYWORD_PATTERNS
)

#: A sentence ends at ``.``, ``?`` or ``!`` followed by whitespace. Transcribe
#: Medical punctuates its output, so this usually finds real boundaries; when it
#: does not, the whole segment is treated as one sentence, which is the right
#: degradation.
_SENTENCE_BOUNDARY: Final = re.compile(r"(?<=[.!?])\s+")

#: How many sentences of context travel with a match: the one containing the
#: keyword, plus the one before it. TASK-021 says "the sentence or two
#: containing the keyword". The preceding sentence is the one that usually
#: carries the indication — "the knee has been locking for three months. Let's
#: get an MRI." — which is exactly what the gap analysis needs to see.
_CONTEXT_SENTENCES: Final = 2


@dataclass(frozen=True)
class ProcedureMention:
    """One procedure keyword found in one segment, with its surrounding text.

    Attributes:
        keyword: The canonical keyword, from the fixed list — ``"MRI"``,
            ``"referral"``. This is the identity used to suppress repeat
            mentions within a session, so it must not vary with how the
            clinician phrased it.
        procedure: What to call the procedure when querying, as close to what
            was said as the list allows: ``"MRI"``, or ``"referral to
            orthopedics"`` where a specialist was named.
        matched_text: The exact substring that matched. PHI, like the rest.
        excerpt: The sentence containing the match and the sentence before it.
            This is what becomes ``clinical_context`` on the policy query.
    """

    keyword: str
    procedure: str
    matched_text: str
    excerpt: str


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` offsets for each non-empty sentence in `text`.

    Offsets rather than substrings so a match position maps back to its sentence
    without searching the text a second time.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in _SENTENCE_BOUNDARY.finditer(text):
        spans.append((start, boundary.start()))
        start = boundary.end()
    spans.append((start, len(text)))
    return [(begin, end) for begin, end in spans if text[begin:end].strip()]


def extract_context(text: str, position: int) -> str:
    """Return the sentence containing `position` and the one before it.

    Args:
        text: The full segment text.
        position: A character offset inside `text`, normally the start of a
            keyword match.

    Returns:
        The excerpt, stripped. Falls back to the whole segment when the text
        carries no sentence punctuation, which is the useful answer rather than
        an empty one.

    **The context never crosses segment boundaries**, because this service sees
    one segment at a time and holds no transcript. A keyword in the opening
    words of a segment therefore carries whatever preceded it in *that* segment
    and nothing earlier. Accumulating a rolling transcript is TASK-030's job in
    track-a-clinical, and a second buffer here would mean two services holding
    the same PHI in memory for the length of an encounter.
    """
    spans = _sentence_spans(text)
    if not spans:
        return text.strip()

    index = next(
        (i for i, (begin, end) in enumerate(spans) if begin <= position < end),
        len(spans) - 1,
    )
    first = max(0, index - (_CONTEXT_SENTENCES - 1))
    return text[spans[first][0] : spans[index][1]].strip()


def _referral_mention(text: str) -> tuple[int, ProcedureMention] | None:
    """Return the first ``referral to <specialist>`` mention and its position."""
    match = _REFERRAL_PATTERN.search(text)
    if match is None:
        return None

    specialist = " ".join(match.group("specialist").split()).lower()
    return match.start(), ProcedureMention(
        keyword=_REFERRAL_KEYWORD,
        procedure=f"referral to {specialist}",
        matched_text=match.group(0),
        excerpt=extract_context(text, match.start()),
    )


def detect_procedures(text: str) -> list[ProcedureMention]:
    """Return every distinct procedure keyword in one transcript segment.

    Args:
        text: One stabilized transcript segment.

    Returns:
        One mention per distinct keyword, ordered by where each first appears.
        A keyword named twice in the same segment yields one mention — the
        repeat says nothing new, and every mention costs a policy query.
        Repeats across *different* segments are suppressed too, but that is the
        session-scoped guard in :mod:`track_b_rag.dedup` rather than anything
        this function can see.
    """
    found: list[tuple[int, ProcedureMention]] = []

    for keyword, pattern in _COMPILED:
        match = pattern.search(text)
        if match is None:
            continue
        found.append(
            (
                match.start(),
                ProcedureMention(
                    keyword=keyword,
                    procedure=keyword,
                    matched_text=match.group(0),
                    excerpt=extract_context(text, match.start()),
                ),
            )
        )

    referral = _referral_mention(text)
    if referral is not None:
        found.append(referral)

    return [mention for _, mention in sorted(found, key=lambda item: item[0])]
