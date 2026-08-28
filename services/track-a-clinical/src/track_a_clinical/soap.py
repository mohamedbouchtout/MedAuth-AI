"""Turning an accumulated transcript into a SOAP note and a set of codes.

Two Bedrock calls, deliberately, per TASK-030 and CLAUDE.md's Bedrock Model
Assignment table: Sonnet writes the note, Haiku extracts the ICD-10 and CPT
codes. Asking one model to do both would put the expensive model on mechanical
extraction — the table's whole point is that extraction costs about a fifteenth
as much on Haiku — and would entangle two failures that are better kept apart,
for the reason :func:`generate` gives.

The two calls are independent functions of the same transcript, so they run
concurrently. TASK-060 waits on the row this produces before it can assemble a
bundle, and a session that has already ended is a session whose provider is
waiting.

**Everything in this module is PHI.** The prompts contain the transcript of a
clinical encounter and the answers contain a clinical note. Neither is ever
logged, put in an exception message, or included in an error returned to a
caller — log lines here carry session ids, model names and counts. The
transcript reaches Bedrock, which is the HIPAA-eligible path, and nowhere else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bedrock_client import first_json_object
from track_a_clinical import bedrock
from track_a_clinical.models import ExtractedCode

logger = logging.getLogger(__name__)

#: One retry, matching track-b-rag's policy analysis. The theory is the same: a
#: malformed answer from a deterministic model is more likely a long generation
#: that hit the token ceiling than a prompt the model cannot follow, and a
#: second attempt is cheap next to losing the encounter's note. More than one
#: retry would spend real money re-reading a long transcript.
MAX_ATTEMPTS: Final = 2


class SoapSections(BaseModel):
    """The four sections of a SOAP note, as the model is asked to return them."""

    model_config = ConfigDict(extra="ignore")

    subjective: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    assessment: str = Field(min_length=1)
    plan: str = Field(min_length=1)


class _ExtractedCodeAnswer(BaseModel):
    """One code as the extraction model returns it, before it becomes an entry.

    Separate from :class:`~track_a_clinical.models.ExtractedCode` on purpose:
    this is what a model may say, and that is what the column may hold. Anything
    the model volunteers beyond a code and a description — a confidence it was
    not asked for, most of all — is dropped here rather than reaching the
    column, where it would be indistinguishable from a measured score.
    """

    model_config = ConfigDict(extra="ignore")

    code: str = Field(min_length=1)
    display: str | None = None


class _ExtractionAnswer(BaseModel):
    """Both code lists, as the extraction model is asked to return them."""

    model_config = ConfigDict(extra="ignore")

    icd10_codes: list[_ExtractedCodeAnswer] = Field(default_factory=list)
    cpt_codes: list[_ExtractedCodeAnswer] = Field(default_factory=list)


@dataclass(frozen=True)
class GeneratedNote:
    """What one generation produced.

    ``icd10_codes`` and ``cpt_codes`` are ``None`` when the extraction pass
    failed and an empty list when it ran and found nothing. The column keeps
    that distinction — NULL against ``[]`` — for the same reason an entry's
    ``validation`` does: "not determined" and "determined to be none" are
    different facts, and a note whose codes are missing because Haiku was
    unreachable must not read as an encounter with no diagnosis in it.
    """

    sections: SoapSections
    icd10_codes: list[ExtractedCode] | None
    cpt_codes: list[ExtractedCode] | None


SOAP_PROMPT: Final = """You are a clinical documentation assistant. Write a
SOAP note from the transcript of a physician-patient encounter below.

Rules:
- Use only what the transcript supports. Do not infer findings, vitals,
  measurements or history that were not stated.
- If a section has no supporting content, say so plainly in that section rather
  than inventing content or leaving it empty.
- Write in the clinical register a physician would use, in the third person.
- Do not include the patient's name or any identifier in the note body.

Return a single JSON object and nothing else, with exactly these keys:
{{"subjective": "...", "objective": "...", "assessment": "...", "plan": "..."}}

Transcript:
{transcript}
"""

EXTRACTION_PROMPT: Final = """Extract billing codes from the transcript of a
physician-patient encounter below.

Rules:
- ICD-10-CM codes for the diagnoses actually discussed, with their dots.
- CPT codes for procedures ordered or anticipated during this encounter.
- Only codes the transcript supports. An empty list is the correct answer when
  nothing was discussed that carries a code.
- Do not report a confidence, certainty or score for any code.

Return a single JSON object and nothing else, with exactly these keys:
{{"icd10_codes": [{{"code": "M17.11", "display": "..."}}],
"cpt_codes": [{{"code": "73721", "display": "..."}}]}}

Transcript:
{transcript}
"""


def build_soap_prompt(transcript: str) -> str:
    """Return the Sonnet prompt for one encounter's transcript."""
    return SOAP_PROMPT.format(transcript=transcript)


def build_extraction_prompt(transcript: str) -> str:
    """Return the Haiku prompt for one encounter's transcript."""
    return EXTRACTION_PROMPT.format(transcript=transcript)


def _parse_document(answer: str, model: type[BaseModel]) -> Any:
    """Return `answer`'s JSON document parsed as `model`, or None if unusable.

    Tolerates a model that fenced the JSON or prefaced it with a sentence, which
    :func:`bedrock_client.first_json_object` handles. It does not tolerate a
    document of the wrong shape — that is what the retry exists for.
    """
    document = first_json_object(answer)
    if document is None:
        return None
    try:
        parsed = json.loads(document)
    except ValueError:
        return None
    try:
        return model.model_validate(parsed)
    except ValidationError:
        # Covers a document of the wrong shape and one that is not an object at
        # all; both raise ValidationError out of model_validate.
        return None


def parse_soap(answer: str) -> SoapSections | None:
    """Return the SOAP sections in a model answer, or None if it is unusable."""
    result = _parse_document(answer, SoapSections)
    return result if isinstance(result, SoapSections) else None


def parse_extraction(answer: str) -> tuple[list[ExtractedCode], list[ExtractedCode]] | None:
    """Return the (ICD-10, CPT) entries in a model answer, or None if unusable.

    Every entry comes back as ``source: "llm-extraction"`` with no confidence,
    which :class:`~track_a_clinical.models.ExtractedCode` enforces — a model is
    not asked to rate itself and could not be believed if it did.
    """
    result = _parse_document(answer, _ExtractionAnswer)
    if not isinstance(result, _ExtractionAnswer):
        return None
    try:
        icd10 = [ExtractedCode.from_llm(entry.code, entry.display) for entry in result.icd10_codes]
        cpt = [ExtractedCode.from_llm(entry.code, entry.display) for entry in result.cpt_codes]
    except ValidationError:
        # A blank code, which from_llm rejects. The whole answer is unusable
        # rather than one entry silently dropped from a list whose completeness
        # is the thing being extracted.
        return None
    return icd10, cpt


async def _attempt[T](
    invoke: Callable[[str], Awaitable[str]],
    prompt: str,
    parse: Callable[[str], T | None],
    *,
    what: str,
    session_id: uuid.UUID,
) -> T | None:
    """Invoke a model until it returns something parseable, or give up.

    Returns None when every attempt failed, whether the call raised or the
    answer would not parse. ``what`` names the pass in log lines; neither the
    prompt nor the answer is ever logged, because both carry the encounter.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            answer = await invoke(prompt)
        except Exception:
            logger.error(
                "Bedrock %s call failed for session %s (attempt %d of %d)",
                what,
                session_id,
                attempt,
                MAX_ATTEMPTS,
                exc_info=True,
            )
            continue

        parsed = parse(answer)
        if parsed is not None:
            return parsed

        logger.warning(
            "Bedrock returned an unusable %s answer for session %s (attempt %d of %d)",
            what,
            session_id,
            attempt,
            MAX_ATTEMPTS,
        )
    return None


async def generate(transcript: str, *, session_id: uuid.UUID) -> GeneratedNote | None:
    """Generate a note and its codes from one encounter's transcript.

    Returns None when the SOAP pass failed, which is the case where there is
    nothing worth storing — the caller keeps its buffer and the encounter can be
    retried.

    A failed *extraction* pass does not fail the generation. The note is what a
    provider reviews and what TASK-032 serves, and withholding it because a code
    list is missing loses more than it protects; the codes come back as None so
    the column records that they were never determined rather than that the
    encounter had none. TASK-060 reads ``icd10_codes`` for a bundle's diagnoses
    and finds nothing there, which is true and visible, rather than an empty
    list that reads as a settled answer.
    """
    if not transcript.strip():
        # Nothing was said, or nothing was transcribed. A prompt containing an
        # empty transcript invites the model to invent an encounter.
        logger.warning("No transcript accumulated for session %s; nothing to generate", session_id)
        return None

    sections, extraction = await asyncio.gather(
        _attempt(
            bedrock.invoke_reasoning,
            build_soap_prompt(transcript),
            parse_soap,
            what="SOAP",
            session_id=session_id,
        ),
        _attempt(
            bedrock.invoke_fast,
            build_extraction_prompt(transcript),
            parse_extraction,
            what="code extraction",
            session_id=session_id,
        ),
    )

    if sections is None:
        logger.error(
            "No SOAP note generated for session %s after %d attempts",
            session_id,
            MAX_ATTEMPTS,
        )
        return None

    if extraction is None:
        logger.error(
            "No codes extracted for session %s; storing the note with codes unset",
            session_id,
        )
        return GeneratedNote(sections=sections, icd10_codes=None, cpt_codes=None)

    icd10, cpt = extraction
    logger.info(
        "Generated a note for session %s with %d ICD-10 and %d CPT codes",
        session_id,
        len(icd10),
        len(cpt),
    )
    return GeneratedNote(sections=sections, icd10_codes=icd10, cpt_codes=cpt)
