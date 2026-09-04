"""The provider registry and this client have to agree — proven, not assumed.

TASK-025b, on exactly the terms ``test_note_contract.py`` sets out for TASK-053.
``track-a-clinical`` serves ``POST /providers/resolve``; ``src/providers_client``
mirrors its request and response so it can call it. Every other test in this
suite feeds that client a hand-written body, which proves it parses what the
tests expect and cannot prove it parses what the other service actually sends —
both sides can drift together into a shape no deployment produces.

The failure this exists to catch is narrow and quiet: a renamed field on either
side leaves the launch context answering a null ``provider_id`` for every
launch, which is *also* what a launch with an unverified actor answers. So the
symptom of a deployment mismatch is indistinguishable from a normal, expected
state, and nothing errors anywhere.

``.github/scripts/detect-changed-members.sh`` selects this service when
``track-a-clinical`` changes, so an edit on the registry's side re-runs this file
rather than leaving it decorative.
"""

from __future__ import annotations

import uuid

import pytest

from src.providers_client import ResolvedProvider
from track_a_clinical.api.schemas import ProviderData, ResolveProviderRequest


def test_the_client_parses_what_the_registry_returns() -> None:
    """Built from the registry's own response model, with nothing hand-written."""
    served = ProviderData(provider_id=uuid.uuid4())

    parsed = ResolvedProvider.model_validate(served.model_dump(mode="json"))

    assert parsed.provider_id == str(served.provider_id)


def test_the_registry_accepts_what_the_client_sends() -> None:
    """The other direction: this client's body has to satisfy that request model.

    ``extra="forbid"`` on the registry's side means an added field here is a 422
    rather than something quietly ignored, so the assertion is worth making.
    """
    reference = "https://ehr.example-hospital.org/r4/Practitioner/prov-77"

    accepted = ResolveProviderRequest.model_validate({"fhir_practitioner_ref": reference})

    assert accepted.fhir_practitioner_ref == reference


def test_an_absolute_reference_fits_the_field() -> None:
    """A real vendor reference is a URL, and the column is 512 characters.

    Checked against the request model rather than the column, because that is
    where an over-long reference would first be refused — and a refusal there is
    what the client would see as "the registry refused the request", with no clue
    that the cause was length.
    """
    long_reference = "https://" + "a" * 460 + ".example.org/r4/Practitioner/1"
    assert len(long_reference) <= 512

    assert (
        ResolveProviderRequest.model_validate(
            {"fhir_practitioner_ref": long_reference}
        ).fhir_practitioner_ref
        == long_reference
    )

    with pytest.raises(ValueError):
        ResolveProviderRequest.model_validate({"fhir_practitioner_ref": "x" * 513})
