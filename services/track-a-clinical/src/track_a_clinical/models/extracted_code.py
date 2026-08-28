"""The JSON shape stored in ``clinical_notes.icd10_codes`` and ``cpt_codes``.

Not a mapped class — a Pydantic model for what goes *inside* two JSONB columns
on :class:`~track_a_clinical.models.clinical_note.ClinicalNote`. It lives beside
the mapped classes for the reason CLAUDE.md gives for those: four consumers read
or write this shape and the alternative is each of them re-deriving it from the
column. TASK-030 writes it, TASK-031 fills in the validation half, TASK-060
reads ``icd10_codes`` as a bundle's diagnoses, and TASK-072 renders both.

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

Nothing here is PHI on its own: a diagnosis code attached to an encounter is,
which is why the rows carrying it are audited, but the model is just a shape.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Written by TASK-030's Haiku pass.
SOURCE_LLM_EXTRACTION: Final = "llm-extraction"
#: Written by TASK-031's Comprehend Medical pass.
SOURCE_COMPREHEND_MEDICAL: Final = "comprehend-medical"

CodeSource = Literal["llm-extraction", "comprehend-medical"]

#: A probability, and only ever one a source actually reported.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


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
        """Uppercase and strip, so every later comparison is string equality.

        The same reasoning as the payer slug in CLAUDE.md: a code matched by
        exact equality anywhere must be normalised everywhere it is written, or
        the mismatch is silent.
        """
        canonical = value.strip().upper()
        if not canonical:
            raise ValueError("code must not be blank")
        return canonical

    @model_validator(mode="after")
    def _llm_extractions_report_no_confidence(self) -> Self:
        """Reject a confidence attached to an LLM extraction.

        Enforced rather than documented: the whole value of the column is that a
        score in it came from something that measures. A caller that wants to
        record a model's self-assessment is asking for the field to mean two
        things at once.
        """
        if self.source == SOURCE_LLM_EXTRACTION and self.confidence is not None:
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
