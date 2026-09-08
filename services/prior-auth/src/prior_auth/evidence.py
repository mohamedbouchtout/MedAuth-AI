"""Building ``clinical_evidence`` from the provider's note and the nudges.

**This is the part of TASK-060 that was respecified before it was built**, so
the reasoning is here rather than only in TASKS.md.

The task originally said ``clinical_evidence`` holds "relevant transcript
excerpts ... the segments tied to flagged procedures". It does not, and must
not:

* **The note is the better artifact, and that is the deciding reason.** A
  prior-auth bundle asserts to a payer what the provider *documented*. The SOAP
  note is exactly that — the provider reviews it, edits it through TASK-032's
  ``PATCH /notes/{session_id}`` and attests to it with ``reviewed_by_provider``.
  A raw conversational excerpt is none of those things: nobody signed it, it is
  not framed for a payer, and it carries whatever else was said in the room.
  This is CLAUDE.md's "Writing clinical data out to the EHR" rule — nothing a
  machine merely surfaced leaves this system as though a provider stated it —
  applied to evidence as well as to codes.
* **A new PHI store does not clear the bar.** Excerpting speech would mean
  persisting it, and TASK-030 deliberately declined to: its ``TranscriptBuffer``
  is in-memory, and that task accepts losing a visit in flight to a restart
  rather than putting raw encounter speech in a second place for the length of
  every visit.
* **The transcript is also unreachable here, which is the lesser reason.** The
  buffer is dropped once the note row commits, in another service's process,
  and this service waits for that same row — so it starts looking exactly when
  the last copy is gone. Even if it were reachable, the two points above still
  decide it.

So: **do not "fix" this later by adding transcript persistence.** The note is
not a fallback for a transcript we cannot reach.

What an entry is
----------------
The wire shape is fixed by TASK-054, which already consumes this column:
``{"text": str, "criterion": str | None}`` — see ``StoredEvidence`` in
``fhir-integration``'s ``prior_auth_client``. Each entry becomes one
``Claim.supportingInfo``, which is where a payer's criteria are actually
evaluated.

Two kinds of entry are produced, and they answer different questions:

* **A criterion entry**, one per unsatisfied criterion the nudge recorded,
  carrying whatever the note says about that procedure. This is the entry a
  payer's reviewer is looking for.
* **A procedure entry**, carrying the nudge's own message, so a bundle whose
  criteria are unknown still says which procedure it is about. ``missing_criteria``
  is legitimately empty on a fallback answer — meaning the criteria are
  *unknown*, not that none are missing — and an evidence list that went empty
  there would lose the only thing the encounter established.

Narrowing
---------
Only the note sentences that mention the procedure or its CPT code are carried,
never the whole note. That is a HIPAA minimum-necessary decision, not a
payload-size one: a payer is owed the documentation supporting *this* request
and nothing else about the visit.

The matching is deliberately literal — a case-insensitive search for the
procedure as the clinician said it, or for the code — and it therefore
**under-includes**: a note that phrases a procedure differently from the spoken
order yields no excerpt for it. That is the safe direction. A bundle asserting
less than the note says is a gap a reviewer can see and a provider can fill; one
asserting more is a claim nobody made. Widening this with fuzzy or model-driven
matching is a separate decision, and it would need to answer for both.
"""

from __future__ import annotations

import re
from typing import Any, Final

from track_a_clinical.models import ClinicalNote, ClinicalNudge

#: The note fields searched for supporting text, in the order they appear in a
#: SOAP note. All four are included rather than just assessment and plan: the
#: conservative-therapy and prior-imaging criteria payers ask about are recorded
#: as history, which lives in the subjective section.
NOTE_SECTIONS: Final = (
    "soap_subjective",
    "soap_objective",
    "soap_assessment",
    "soap_plan",
)

#: Splits a section into sentences. Deliberately simple: a period, question mark
#: or exclamation mark followed by whitespace, or a line break. Clinical prose
#: abbreviates heavily ("2 mg.", "Dr. Chen"), so a stricter regex would split
#: mid-sentence and a looser one would return the whole section — and the cost
#: of an over-long excerpt here is a reviewer reading one extra clause, while
#: the cost of a mangled one is documentation that reads as incoherent.
_SENTENCE_BREAK: Final = re.compile(r"(?<=[.!?])\s+|\n+")

#: Bounds one excerpt. A section written as a single unbroken paragraph would
#: otherwise be carried whole, which is exactly the widening this module
#: refuses. Truncated on a word boundary with an ellipsis so a reader can see
#: that it was cut rather than that the note stopped there.
MAX_EXCERPT_CHARS: Final = 600

#: Words dropped when a criterion is turned into search terms. Not a general
#: stopword list — only the connectives and quantifiers that appear in payer
#: criteria and carry no clinical meaning. Kept deliberately short: a longer
#: list is a longer thing to be wrong about, and a word left in only costs a
#: term that matches nothing.
_CRITERION_STOPWORDS: Final = frozenset(
    {
        "and",
        "any",
        "are",
        "been",
        "documented",
        "for",
        "has",
        "have",
        "least",
        "must",
        "not",
        "the",
        "was",
        "were",
        "with",
        "within",
    }
)

#: The shortest word taken as a criterion search term. Three characters and
#: under are overwhelmingly connectives in this vocabulary ("of", "at", "six"),
#: and a term that short matches inside unrelated words.
_MIN_CRITERION_TERM_CHARS: Final = 4

#: How many distinct criterion terms a sentence must carry to be offered against
#: that criterion. **A chosen bound, not a measurement.** One is too loose — a
#: single common word like "therapy" would pull in any sentence mentioning any
#: treatment — and three would miss criteria that are only two words long once
#: the stopwords are dropped. Two is the smallest value that requires a sentence
#: to be about the criterion rather than merely to share a word with it.
MIN_CRITERION_TERM_MATCHES: Final = 2

#: Splits a sentence into comparable words. Same normalisation on both sides of
#: the comparison, which is the rule this repository already applies to payer
#: slugs and ICD-10 codes: neither side may normalise independently.
_WORD: Final = re.compile(r"[a-z0-9]+")


def _sentences(text: str) -> list[str]:
    """Split one note section into candidate excerpts, dropping empties."""
    return [sentence.strip() for sentence in _SENTENCE_BREAK.split(text) if sentence.strip()]


def _words(text: str) -> set[str]:
    """Return the comparable words of a sentence or criterion."""
    return set(_WORD.findall(text.casefold()))


def criterion_terms(criterion: str) -> frozenset[str]:
    """Return the words that make a note sentence relevant to one criterion.

    **Why a criterion needs its own terms at all.** The sentence that documents
    "six weeks of conservative therapy" is about the therapy, not about the MRI
    — a note records it as history, and it need never name the procedure. So
    matching on the procedure alone finds the sentences that *order* the
    procedure and misses the ones that *justify* it, which is precisely the
    documentation a payer evaluates the criterion against.
    """
    return frozenset(
        word
        for word in _words(criterion)
        if len(word) >= _MIN_CRITERION_TERM_CHARS and word not in _CRITERION_STOPWORDS
    )


def _truncate(text: str) -> str:
    """Return `text` bounded to :data:`MAX_EXCERPT_CHARS`, cut on a word boundary."""
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    cut = text[:MAX_EXCERPT_CHARS].rsplit(" ", 1)[0].rstrip()
    return f"{cut or text[:MAX_EXCERPT_CHARS].rstrip()}…"


def _search_terms(nudge: ClinicalNudge) -> list[str]:
    """Return what a note sentence has to mention to be about this nudge.

    The procedure as the clinician said it, and the CPT code it resolved to.
    Either is enough: a note may name the procedure in words, or cite the code,
    and both are the provider documenting the same order.
    """
    terms = []
    if nudge.procedure_name and nudge.procedure_name.strip():
        terms.append(nudge.procedure_name.strip().casefold())
    if nudge.cpt_code and nudge.cpt_code.strip():
        terms.append(nudge.cpt_code.strip().casefold())
    return terms


def note_excerpts(
    note: ClinicalNote | None,
    nudge: ClinicalNudge,
    *,
    criterion: str | None = None,
) -> list[str]:
    """Return the note sentences supporting this nudge, and this criterion.

    A sentence is carried when it names the procedure — by the clinician's own
    words or by its CPT code — **or** when it carries at least
    :data:`MIN_CRITERION_TERM_MATCHES` of the criterion's own terms. Both halves
    are needed and they find different things: the first finds the sentences
    that *order* the procedure, the second the ones that *justify* it, which are
    usually recorded as history and need never name it.

    Args:
        note: The encounter's generated note, or None when there is none.
        nudge: The nudge whose procedure the excerpts must be about.
        criterion: The payer criterion these excerpts are offered against, when
            there is one. None for the entry carrying the nudge's own message,
            which is about the procedure as a whole.

    Returns:
        The matching sentences in note order, deduplicated. Empty when the note
        documents nothing relevant, which is a meaningful answer rather than a
        failure — it is what an undocumented order looks like, and it is the
        situation the nudge fired about in the first place.
    """
    procedure_terms = _search_terms(nudge)
    terms_for_criterion = criterion_terms(criterion) if criterion else frozenset()
    if note is None or (not procedure_terms and not terms_for_criterion):
        return []

    found: list[str] = []
    for section in NOTE_SECTIONS:
        text = getattr(note, section, None)
        if not text:
            continue
        for sentence in _sentences(str(text)):
            haystack = sentence.casefold()
            names_procedure = any(term in haystack for term in procedure_terms)
            supports_criterion = (
                len(terms_for_criterion & _words(sentence)) >= MIN_CRITERION_TERM_MATCHES
            )
            if (names_procedure or supports_criterion) and sentence not in found:
                found.append(_truncate(sentence))
    return found


def build_evidence(
    note: ClinicalNote | None,
    nudges: list[ClinicalNudge],
) -> list[dict[str, Any]]:
    """Assemble the ``clinical_evidence`` column for one encounter.

    Args:
        note: The encounter's generated SOAP note, or None. None is tolerated
            rather than rejected because the caller decides whether to assemble
            without one; this function's job is only to say what the evidence
            is, and with no note the answer is "the nudges and nothing more".
        nudges: The nudges fired during the encounter, in the order they fired.

    Returns:
        Entries in ``{"text": ..., "criterion": ...}`` form — the shape
        TASK-054's submitter already reads. One per unsatisfied criterion, plus
        one carrying the nudge's own message, per procedure.
    """
    entries: list[dict[str, Any]] = []

    for nudge in nudges:
        for criterion in nudge.missing_criteria or ():
            entries.append(
                {
                    # The documentation offered against this criterion,
                    # excerpted per criterion rather than once per procedure —
                    # each criterion is evaluated on its own by the payer, and a
                    # shared blob would offer every criterion the evidence for
                    # all the others. Empty when the note says nothing relevant;
                    # carried anyway, because "this criterion is unmet and
                    # undocumented" is the substance of the request rather than
                    # an absence to hide.
                    "text": " ".join(note_excerpts(note, nudge, criterion=criterion)),
                    "criterion": criterion,
                }
            )

        if nudge.nudge_message:
            entries.append(
                {
                    # Not tied to one criterion: this is what the encounter
                    # established about the procedure as a whole, and it is the
                    # only entry when the criteria came back unknown.
                    "text": _truncate(nudge.nudge_message),
                    "criterion": None,
                }
            )

    return entries
