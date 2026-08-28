"""Hand-written stand-ins for AWS Comprehend Medical ``InferICD10CM`` responses.

**These are synthetic. Nobody has run them against the real service.**

Moto does not implement Comprehend Medical at all — a live ``@mock_aws`` call to
``InferICD10CM`` returns ``404 Not yet implemented``, because moto's
``comprehend`` module is Amazon Comprehend, a different service. CLAUDE.md
carries that as a standing exception to the "moto for all AWS mocking" rule and
permits exactly two alternatives: a real credentialed call behind an env gate, or
explicitly-labelled synthetic fixtures. This module is the second, and this
docstring is the label.

**What is faithful here** is the response *structure*, which is taken from
botocore's own service model for the operation rather than from prose docs:
``Entities`` is a list; each entity carries ``Score``, ``Text``, ``Category``,
``Type`` and a nested ``ICD10CMConcepts`` list; each concept carries
``Description``, ``Code`` and its own ``Score``. Both score fields are real and
distinct, which is the distinction :func:`track_a_clinical.comprehend._best_scores`
turns on.

**What is UNVERIFIED** is the format of ``ICD10CMConcept.Code``. ICD-10-CM has
two equally standard spellings of any code with an extension — ``M17.11`` and
``M1711`` — and botocore types the field as an unconstrained string, so the API
contract cannot settle which one the service emits. These fixtures deliberately
contain **both** spellings, so the tests fail if the production code ever stops
normalising and starts depending on one of them. Running
``scratchpad/probe_real.py`` against real credentials produces a live response
and closes the question; until then, do not "correct" one of these spellings to
match the other.

The clinical content is invented and describes no real person.
"""

from __future__ import annotations

from typing import Any


def entity(
    text: str,
    *,
    concepts: list[tuple[str, float]],
    entity_score: float = 0.99,
    category: str = "MEDICAL_CONDITION",
) -> dict[str, Any]:
    """Build one ``ICD10CMEntity``, with its concepts as ``(code, score)`` pairs.

    ``entity_score`` defaults high and deliberately differs from the concept
    scores: a test that accidentally reads the entity-level ``Score`` instead of
    the concept score gets a visibly wrong number rather than a coincidentally
    right one.
    """
    return {
        "Id": 0,
        "Text": text,
        "Category": category,
        "Type": "DX_NAME",
        "Score": entity_score,
        "BeginOffset": 0,
        "EndOffset": len(text),
        "Attributes": [],
        "Traits": [],
        "ICD10CMConcepts": [
            {"Description": f"synthetic description for {code}", "Code": code, "Score": score}
            for code, score in concepts
        ],
    }


def response(*entities: dict[str, Any]) -> dict[str, Any]:
    """Wrap entities in the envelope ``InferICD10CM`` returns."""
    return {
        "Entities": list(entities),
        "ModelVersion": "synthetic-fixture",
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }


#: Three clear diagnoses, all confidently linked. Note the deliberate mix of
#: dotted and dotless codes — see this module's docstring.
THREE_CLEAR_DIAGNOSES: dict[str, Any] = response(
    entity("osteoarthritis of the right knee", concepts=[("M17.11", 0.94)]),
    entity("type 2 diabetes", concepts=[("E119", 0.91)]),
    entity("essential hypertension", concepts=[("I10", 0.97)]),
)

#: One entity Comprehend detected confidently but linked to a code only weakly —
#: the case the entity/concept score distinction exists for.
WEAKLY_LINKED: dict[str, Any] = response(
    entity("some joint discomfort", concepts=[("M17.11", 0.42)], entity_score=0.99),
)

#: Comprehend found nothing at all. A real possibility for a short or
#: non-clinical stretch of transcript.
NOTHING_FOUND: dict[str, Any] = response()
