"""Checking the LLM's ICD-10 codes against AWS Comprehend Medical.

TASK-030's Haiku pass proposes ICD-10-CM codes from an encounter transcript.
This module asks a second, independent source — Comprehend Medical's
``InferICD10CM`` — what codes it finds in the same transcript, and records the
answer in the ``validation`` half of each entry per CLAUDE.md "Extracted
clinical codes — one JSON shape".

**The transcript is the input, never the generated note.** Validating the LLM's
codes against text the same LLM wrote measures self-consistency rather than
accuracy: the note is where the model already committed to its reading of the
encounter, so it would agree with itself and confirm nothing. The transcript is
the independent source both passes derive from.

**This pass is never fatal.** A Comprehend failure — throttling, an outage, a
text-size rejection — leaves every entry's ``validation`` at ``None`` and the
note is stored anyway. The note is what the provider is waiting for; validation
is secondary metadata about it, and blocking the first on the second trades the
thing that matters for the thing that annotates it. ``validation is None``
already means exactly "not checked yet", so an unvalidated note needs no extra
signalling to be read correctly.

**Everything passed to this module is PHI.** The transcript reaches Comprehend
Medical, which is HIPAA-eligible, and nowhere else. Log lines here carry codes,
confidence scores and counts — never the transcript, never an entity's matched
span, and never a code's surrounding text. Discrepancies are logged through
standard :mod:`logging` rather than hipaa-logger: a code disagreeing with a
second opinion is a quality metric, not a PHI access event (CLAUDE.md,
packages/hipaa-logger scope note).

**Tests here cannot use moto**, which does not implement Comprehend Medical at
all — see CLAUDE.md's standing exception. The fixtures are hand-written and say
so.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any, Final

import boto3

from track_a_clinical.config import get_settings
from track_a_clinical.models import (
    SOURCE_LLM_EXTRACTION,
    CodeValidation,
    ExtractedCode,
    matching_key,
)

logger = logging.getLogger(__name__)

#: The score at or above which Comprehend is taken to confirm a code.
#:
#: **This is an unvalidated initial guess, not a measured value.** It comes from
#: TASK-031's original wording and has never been checked against a real
#: distribution of ``ICD10CMConcept.Score`` values — nobody here has seen one.
#: It could easily be far too strict or far too lax. Revisit it once
#: ``scratchpad/probe_real.py`` has produced live output; treat it as
#: provisional in the meantime, exactly as CLAUDE.md treats
#: ``SESSION_REMINT_GRACE_SECONDS``.
CONFIRMATION_THRESHOLD: Final = 0.8

#: The hard limit on one ``InferICD10CM`` request, in characters.
#:
#: Not a guess and not the widely-quoted 20,000: botocore carries the real
#: constraint in its shape metadata for this operation as ``{'min': 1, 'max':
#: 10000}`` and rejects a longer request client-side, and the service raises
#: ``TextSizeLimitExceededException`` behind it. The 20,000-byte figure belongs
#: to ``DetectEntitiesV2``, a different operation; sizing chunks by it would put
#: every chunk at twice the limit and fail on every long visit.
MAX_REQUEST_CHARACTERS: Final = 10_000


@lru_cache(maxsize=1)
def get_client() -> Any:
    """Return the process-wide ``comprehendmedical`` boto3 client.

    Typed ``Any`` for the reason :mod:`track_a_clinical.bedrock` gives: boto3
    builds its clients dynamically and ships no static type for one.

    A raw boto3 client rather than anything async, because Comprehend Medical
    has no async SDK. Every call through it is therefore wrapped — see
    :func:`_infer_icd10`.
    """
    return boto3.client("comprehendmedical", region_name=get_settings().aws_region)


def reset_client() -> None:
    """Forget the cached client. For tests and shutdown."""
    get_client.cache_clear()


def split_for_requests(text: str, limit: int = MAX_REQUEST_CHARACTERS) -> list[str]:
    """Split a transcript into chunks that each fit one request.

    Splits on whitespace so a chunk boundary never lands inside a word, which
    would present Comprehend with two fragments it cannot recognise as the term
    they came from. **Nothing is ever discarded**: every character of non-empty
    content in ``text`` appears in exactly one chunk, which is the property that
    separates chunking from the silent truncation CLAUDE.md forbids.

    A single token longer than ``limit`` — which no transcript of speech
    produces, but a corrupted segment might — is emitted as its own oversized
    chunk rather than being cut. :func:`validate_icd10` reports it as uncovered
    instead of quietly sending a truncated version.

    Args:
        text: The accumulated transcript.
        limit: Maximum characters per chunk.

    Returns:
        The chunks, in order. Empty when ``text`` holds no non-whitespace.
    """
    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        # +1 for the space that will join this word to the previous one.
        addition = len(word) if not current else len(word) + 1
        if current and length + addition > limit:
            chunks.append(" ".join(current))
            current, length = [word], len(word)
        else:
            current.append(word)
            length += addition
    chunks.append(" ".join(current))
    return chunks


def _best_scores(response: dict[str, Any]) -> dict[str, float]:
    """Reduce one ``InferICD10CM`` response to the best score per code.

    The score read is ``ICD10CMConcept.Score`` and deliberately not the
    entity-level ``Score``. The botocore service model separates their meanings:
    the entity score is confidence in the *detection* that a span of text is a
    medical condition, while the concept score is confidence that the entity is
    *linked to that ICD-10-CM concept*. This comparison is code-to-code, so the
    concept score is the one measuring the question being asked — an entity
    score can be high for a correctly-spotted condition that was then linked to
    the wrong code, which is the error this whole module exists to catch.

    Codes are keyed by :func:`~track_a_clinical.models.matching_key`, so a
    concept spelled ``M1711`` and one spelled ``M17.11`` land on one key. See
    that function for what about the real spelling remains unverified.

    One code can be inferred from several spans in a long transcript. The
    highest score wins: a condition mentioned once in passing and once in the
    assessment should be judged on the mention Comprehend read most clearly.
    """
    best: dict[str, float] = {}
    for entity in response.get("Entities", []):
        for concept in entity.get("ICD10CMConcepts", []):
            code = concept.get("Code")
            score = concept.get("Score")
            if not code or score is None:
                continue
            key = matching_key(str(code))
            if key and score > best.get(key, -1.0):
                best[key] = float(score)
    return best


async def _infer_icd10(text: str) -> dict[str, Any]:
    """Call ``InferICD10CM`` once, off the event loop.

    boto3 is synchronous and has no async variant, so calling it directly from
    this service's consumer would block the event loop for the whole round trip
    to AWS — stalling transcript accumulation for every other live encounter on
    the pod. ``asyncio.to_thread`` is the standing rule for raw sync boto3 in an
    async context; see CLAUDE.md.
    """
    client = get_client()
    result: dict[str, Any] = await asyncio.to_thread(client.infer_icd10_cm, Text=text)
    return result


async def validate_icd10(codes: list[ExtractedCode], transcript: str) -> list[ExtractedCode]:
    """Return `codes` with each LLM-extracted entry's ``validation`` filled in.

    Entries are returned in their original order, as new objects — nothing is
    mutated in place. An entry that is not an ``llm-extraction`` is passed
    through untouched, since this pass exists to check what the LLM proposed.

    Codes Comprehend found that the LLM did not are **not** added here. That is
    TASK-031b: proposing a diagnosis nobody stated is a different question from
    checking one that was, and it needs its own decision before anything writes
    such an entry.

    On any failure — a Comprehend error, or a transcript with nothing in it —
    the codes come back exactly as they arrived, every ``validation`` still
    ``None``, meaning "not checked yet". The caller stores the note regardless.

    Args:
        codes: The ICD-10 entries from TASK-030's Haiku pass.
        transcript: The encounter transcript, the independent source.

    Returns:
        The entries, validated where this pass could reach them.
    """
    if not codes:
        return codes

    chunks = split_for_requests(transcript)
    if not chunks:
        logger.warning(
            "No transcript text to validate %d ICD-10 code(s) against; leaving them unchecked",
            len(codes),
        )
        return codes

    oversized = [index for index, chunk in enumerate(chunks) if len(chunk) > MAX_REQUEST_CHARACTERS]
    if oversized:
        # Reported rather than trimmed. A chunk this long means a single token
        # longer than the request limit, which speech does not produce — the
        # honest response is to say what could not be examined.
        logger.warning(
            "%d transcript chunk(s) exceed the %d-character limit and cannot be "
            "validated; coverage is incomplete",
            len(oversized),
            MAX_REQUEST_CHARACTERS,
        )

    best: dict[str, float] = {}
    examined = 0
    for index, chunk in enumerate(chunks):
        if index in oversized:
            continue
        try:
            response = await _infer_icd10(chunk)
        except Exception:
            # No transcript text in the message: the chunk is encounter speech.
            logger.warning(
                "Comprehend Medical rejected chunk %d of %d; validation coverage is incomplete",
                index + 1,
                len(chunks),
                exc_info=True,
            )
            continue
        examined += 1
        for key, score in _best_scores(response).items():
            if score > best.get(key, -1.0):
                best[key] = score

    if examined == 0:
        logger.warning(
            "No transcript chunk could be validated; %d ICD-10 code(s) remain unchecked",
            len(codes),
        )
        return codes

    if examined < len(chunks):
        logger.warning(
            "Validated %d of %d transcript chunks; a code stated only in an "
            "unexamined chunk may be reported unconfirmed",
            examined,
            len(chunks),
        )

    return [_validated(code, best) for code in codes]


def _validated(code: ExtractedCode, best: dict[str, float]) -> ExtractedCode:
    """Return one entry with its validation recorded, logging a discrepancy.

    Three outcomes, and the middle one is the reason ``confidence`` is nullable:

    * Comprehend returned the code at or above the threshold — ``confirmed``
      true, with its score.
    * It returned the code below the threshold — ``confirmed`` false, **with**
      the score. It saw the concept and was unsure.
    * It did not return the code at all — ``confirmed`` false with ``confidence``
      ``None``. It did not see the concept.

    The last two are different facts and are never collapsed. Neither is
    collapsed into ``validation: None``, which means only "not checked yet".
    """
    if code.source != SOURCE_LLM_EXTRACTION:
        return code

    score = best.get(matching_key(code.code))
    confirmed = score is not None and score >= CONFIRMATION_THRESHOLD

    if not confirmed:
        # The code and its score only — never the text they were found in.
        logger.warning(
            "ICD-10 %s was not confirmed by Comprehend Medical (score=%s, threshold=%s)",
            code.code,
            "none" if score is None else f"{score:.3f}",
            CONFIRMATION_THRESHOLD,
        )

    return code.model_copy(
        update={"validation": CodeValidation(confidence=score, confirmed=confirmed)}
    )
