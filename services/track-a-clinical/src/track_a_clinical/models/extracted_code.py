"""The JSON shape stored in ``clinical_notes.icd10_codes`` and ``cpt_codes``.

Not a mapped class — a Pydantic model for what goes *inside* two JSONB columns
on :class:`~track_a_clinical.models.clinical_note.ClinicalNote`. It lives beside
the mapped classes for the reason CLAUDE.md gives for those: four consumers read
or write this shape and the alternative is each of them re-deriving it from the
column. TASK-030 writes it — both the LLM's codes and the ones only
Comprehend Medical found — TASK-031 fills in the validation half, TASK-060 reads
``icd10_codes`` as a bundle's diagnoses, and TASK-071 renders both.

The contract is fixed in CLAUDE.md "Extracted clinical codes — one JSON shape";
this module is the executable half of it. Two of its rules are enforced here
rather than left to call sites:

* **An LLM extraction carries no confidence.** Haiku is not asked to rate its
  own output, because a number a model invents about itself is not a
  measurement and would be indistinguishable from Comprehend Medical's
  calibrated score once both sit in the same column.
* **``validation is None`` means "not checked yet", never "checked and
  rejected".** A code Comprehend Medical actively failed to find is recorded as
  a present ``validation`` with ``confirmed`` false. Collapsing the two would
  make it identical to a code written before TASK-031 existed — the same
  distinction CLAUDE.md draws between a payer's silence and a payer's negative
  determination.
* **A ``comprehend-medical`` entry is a suggestion, not a stated diagnosis.**
  Nobody in the encounter said it and no note asserted it; Comprehend read it
  out of the transcript. It carries a real score and is surfaced for provider
  review, and it is not a diagnosis a prior-auth bundle may claim on the
  provider's behalf. See :meth:`ExtractedCode.from_comprehend` and CLAUDE.md's
  shape contract for what each consumer owes it.
* **A ``provider-accepted`` entry carries neither a confidence nor a
  validation.** It is the one source a human writes, through TASK-032's note
  edit, and it is how a suggestion becomes documentation TASK-060 may claim. A
  human acceptance is a fact rather than a probability, so there is no score to
  record; and there is nothing independent left to validate a provider's own
  documentation against.

Nothing here is PHI on its own: a diagnosis code attached to an encounter is,
which is why the rows carrying it are audited, but the model is just a shape.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Written by TASK-030's Haiku pass.
SOURCE_LLM_EXTRACTION: Final = "llm-extraction"
#: Written by TASK-031's Comprehend Medical pass.
SOURCE_COMPREHEND_MEDICAL: Final = "comprehend-medical"
#: Written only by a provider, through TASK-032's note edit. No automated pass
#: produces this value and none may promote an entry to it.
SOURCE_PROVIDER_ACCEPTED: Final = "provider-accepted"

#: The sources that report no confidence, for opposite reasons: a model's
#: self-rating is not a measurement, and a human's decision is not a probability.
#: Kept as one set because the *rule* is identical even though the arguments for
#: it are not — see the validator below and CLAUDE.md's shape contract.
_UNSCORED_SOURCES: Final = frozenset({SOURCE_LLM_EXTRACTION, SOURCE_PROVIDER_ACCEPTED})

#: An ICD-10-CM code: a letter, two alphanumerics, then up to four more
#: characters of extension. Used only to decide *where the dot belongs* when
#: re-inserting it, never to validate that a code exists — this repository does
#: not hold the ICD-10-CM tabular list and must not reject a real code for
#: failing a hand-written pattern.
_ICD10_SHAPE: Final = re.compile(r"^([A-Z][0-9A-Z]{2})([0-9A-Z]{1,4})$")

#: Everything that is not an alphanumeric — dots, spaces, stray punctuation.
_NON_ALNUM: Final = re.compile(r"[^0-9A-Z]")

CodeSource = Literal["llm-extraction", "comprehend-medical", "provider-accepted"]

#: A probability, and only ever one a source actually reported.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


def _redot(canonical: str) -> str:
    """Return an ICD-10-CM code in dotted form, leaving anything else alone.

    Only a string matching :data:`_ICD10_SHAPE` once its punctuation is removed
    is re-dotted. A CPT code is five digits and does not match, so it passes
    through untouched — which is the intent: CPT has no dot and inserting one
    would corrupt it.
    """
    bare = _NON_ALNUM.sub("", canonical)
    shape = _ICD10_SHAPE.match(bare)
    if shape is None:
        # Not an ICD-10-CM code by shape: a CPT code, or something this function
        # has no business rewriting. Return what the caller had, minus stray
        # whitespace, rather than guessing at a format for it.
        return canonical
    return f"{shape.group(1)}.{shape.group(2)}"


def matching_key(code: str) -> str:
    """Return the dotless key two sources are compared on.

    **UNVERIFIED — see TASK-031.** ICD-10-CM has two standard spellings of every
    code with an extension (``M17.11`` and ``M1711``), and which one AWS
    Comprehend Medical returns in ``ICD10CMConcept.Code`` has never been checked
    against the real service. It cannot be settled from the API contract:
    botocore types that field as an unconstrained string, so the format is data
    rather than schema. Running ``scratchpad/probe_real.py`` against real
    credentials produces a live response and closes this.

    Stripping the dot from both sides is what makes the open question harmless.
    Whichever spelling Comprehend uses, both sides reduce to the same key, so
    the comparison is correct under either answer — this function is defensive
    against the unknown rather than a bet on one outcome. It is deliberately the
    only place either side is normalised: confirming the real format is then a
    change here and nowhere else.

    Do not delete the UNVERIFIED marker without having seen a real response.
    """
    return _NON_ALNUM.sub("", code.strip().upper())


class CodeValidation(BaseModel):
    """What a validating pass found for one proposed code.

    Present only once that pass has run. ``confidence`` is ``None`` when the
    validator returned no matching entity at all, which is different from
    returning one with a low score — the first means it did not see the concept,
    the second means it saw it and was unsure.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["comprehend-medical"] = SOURCE_COMPREHEND_MEDICAL
    confidence: Confidence | None = None
    confirmed: bool


class ExtractedCode(BaseModel):
    """One ICD-10-CM or CPT code, with where it came from and what confirmed it."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    display: str | None = None
    source: CodeSource
    confidence: Confidence | None = None
    validation: CodeValidation | None = None

    @field_validator("code")
    @classmethod
    def _canonicalize(cls, value: str) -> str:
        """Uppercase, strip, and normalise dot placement to the dotted form.

        The same reasoning as the payer slug in CLAUDE.md: a code matched by
        exact equality anywhere must be normalised everywhere it is written, or
        the mismatch is silent. Uppercasing alone was not enough — ICD-10-CM has
        two equally standard spellings of one code, and a source emitting
        ``M1711`` where another emits ``M17.11`` produces two entries for one
        diagnosis with nothing anywhere reporting a problem.

        Stored dotted, because that is what a clinician reads and what TASK-060
        puts in a prior-auth bundle. Comparison between sources goes through
        :func:`matching_key` instead.
        """
        canonical = value.strip().upper()
        if not canonical:
            raise ValueError("code must not be blank")
        return _redot(canonical)

    @model_validator(mode="after")
    def _provider_acceptance_is_not_measured(self) -> Self:
        """Reject a confidence or a validation attached to a provider acceptance.

        Both would be a machine's number describing a human's decision. The
        entry a provider accepts usually arrives carrying Comprehend Medical's
        own score, and forwarding that score under the new source would make it
        read as a measurement of the acceptance — which nothing measured. The
        score is not preserved elsewhere on purpose: what the provider accepted
        is the code, and the note's edit history is where the acceptance is
        recorded.

        ``validation`` is refused for the reason a ``comprehend-medical`` entry
        keeps ``None`` permanently: there is nothing independent left to check a
        provider's own documentation against.
        """
        if self.source != SOURCE_PROVIDER_ACCEPTED:
            return self
        if self.validation is not None:
            raise ValueError(
                "a provider-accepted entry carries no validation — nothing "
                "independent remains to check a provider's own documentation "
                "against, which is why it stays null permanently"
            )
        return self

    @model_validator(mode="after")
    def _llm_extractions_report_no_confidence(self) -> Self:
        """Reject a confidence attached to an LLM extraction or a provider acceptance.

        Enforced rather than documented: the whole value of the column is that a
        score in it came from something that measures. A caller that wants to
        record a model's self-assessment is asking for the field to mean two
        things at once.

        A ``provider-accepted`` entry is refused for a different reason with the
        same shape — a human acceptance is a fact, not a probability — so both
        sources share one check and the message names whichever applies.
        """
        if self.source in _UNSCORED_SOURCES and self.confidence is not None:
            if self.source == SOURCE_PROVIDER_ACCEPTED:
                raise ValueError(
                    "a provider-accepted entry carries no confidence — a human "
                    "acceptance is a fact, not a probability, and forwarding a "
                    "suggestion's score would attach a machine's uncertainty to "
                    "a person's decision"
                )
            raise ValueError(
                "an llm-extraction entry carries no confidence — a model's "
                "self-rating is not a measurement and must not share a column "
                "with Comprehend Medical's score"
            )
        return self

    @classmethod
    def from_llm(cls, code: str, display: str | None = None) -> ExtractedCode:
        """Build the entry TASK-030's Haiku pass writes: unscored and unvalidated."""
        return cls(code=code, display=display, source=SOURCE_LLM_EXTRACTION)

    @classmethod
    def from_comprehend(
        cls,
        code: str,
        display: str | None,
        confidence: float,
    ) -> ExtractedCode:
        """Build an entry Comprehend Medical proposed on its own.

        Used for a code the LLM pass never proposed. It carries a real
        ``confidence`` — Comprehend's own ``ICD10CMConcept.Score`` — which is
        the field an ``llm-extraction`` entry structurally cannot have, so the
        two kinds of entry stay distinguishable by more than a label.

        **``validation`` stays ``None`` on these entries permanently, and that
        is not the same "not checked yet" a fresh LLM entry carries.** There is
        nothing left to check it against: asking Comprehend to validate a code
        Comprehend proposed measures self-consistency, the same circularity that
        stops the validation pass from being handed the generated note instead
        of the transcript. The honest reading of the field here is "no
        independent source has weighed in", which is exactly what ``None`` says.

        ``display`` is Comprehend's own ``Description`` and is never invented to
        fill the field, per CLAUDE.md's shape contract.
        """
        return cls(
            code=code,
            display=display,
            source=SOURCE_COMPREHEND_MEDICAL,
            confidence=confidence,
        )


def dump_codes(codes: list[ExtractedCode]) -> list[dict[str, Any]]:
    """Render entries for the JSONB column.

    ``mode="json"`` so the result holds only JSON scalars — asyncpg encodes the
    column from this, and a stray Python object would fail at the driver rather
    than at the model.
    """
    return [code.model_dump(mode="json") for code in codes]


def load_codes(raw: object) -> list[ExtractedCode]:
    """Parse a column value back into entries, tolerating an empty column.

    ``icd10_codes`` and ``cpt_codes`` are nullable, and a note generated from a
    transcript naming no diagnosis legitimately has none.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("a code column holds a JSON array of objects")
    return [ExtractedCode.model_validate(entry) for entry in raw]
