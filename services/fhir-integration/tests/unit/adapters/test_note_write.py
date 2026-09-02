"""The adapter's first write to an EHR — what it sends, and how creates fail. TASK-053.

``_get`` had one shape of answer to interpret. A create has two more: the id
comes back in a header rather than a body, and "accepted, but we cannot tell you
what was created" is a real outcome that must not be reported as success — a
made-up id would be recorded against the note as though it named a real
document.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from src.adapters.base import EHRAdapter
from src.adapters.errors import (
    FHIRAuthorizationExpired,
    FHIRMalformedResponse,
    FHIRResourceNotFound,
    FHIRUpstreamUnavailable,
)
from src.adapters.models import ClinicalNoteContent, NoteCode

from .test_note_document import LLM_CODE, SUGGESTED_CODE

FHIR_BASE_URL = "https://ehr.example.org/fhir/r4"
ACCESS_TOKEN = "ehr-access-token"

NOTE = ClinicalNoteContent(
    patient_id="patient-7",
    encounter_id="encounter-4",
    subjective="Right knee pain for three months.",
    assessment="Likely primary osteoarthritis of the right knee.",
    icd10_codes=[LLM_CODE, SUGGESTED_CODE],
)


class CreateServer:
    """A FHIR server that answers one create, however a test tells it to."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.content:
            self.bodies.append(json.loads(request.content))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def adapter(self) -> EHRAdapter:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return EHRAdapter(FHIR_BASE_URL, ACCESS_TOKEN, client)


def created(location: str | None = None, **kwargs: Any) -> httpx.Response:
    """A 201 answering a create, with a ``Location`` unless one is suppressed."""
    headers = {"Location": location} if location is not None else {}
    return httpx.Response(201, headers=headers, **kwargs)


async def test_the_id_comes_from_the_location_header() -> None:
    """R4 gives it as [base]/[type]/[id], optionally with a _history suffix."""
    server = CreateServer(created(f"{FHIR_BASE_URL}/DocumentReference/docref-9/_history/1"))

    assert await server.adapter().write_clinical_note(NOTE) == "docref-9"


async def test_a_location_without_a_history_suffix_also_works() -> None:
    server = CreateServer(created(f"{FHIR_BASE_URL}/DocumentReference/docref-9"))

    assert await server.adapter().write_clinical_note(NOTE) == "docref-9"


async def test_an_echoed_resource_is_accepted_when_there_is_no_location() -> None:
    """Some servers answer with the resource instead. Both are conformant."""
    server = CreateServer(
        created(json={"resourceType": "DocumentReference", "id": "docref-echoed"})
    )

    assert await server.adapter().write_clinical_note(NOTE) == "docref-echoed"


async def test_an_accepted_write_that_names_nothing_is_malformed() -> None:
    """Not a success with an invented id, which would be recorded as a real document."""
    server = CreateServer(created(json={"resourceType": "DocumentReference"}))

    with pytest.raises(FHIRMalformedResponse):
        await server.adapter().write_clinical_note(NOTE)


async def test_a_location_naming_another_resource_type_is_not_trusted() -> None:
    server = CreateServer(created(f"{FHIR_BASE_URL}/Binary/some-other-thing"))

    with pytest.raises(FHIRMalformedResponse):
        await server.adapter().write_clinical_note(NOTE)


async def test_a_rejected_token_ends_the_launch() -> None:
    server = CreateServer(httpx.Response(401))

    with pytest.raises(FHIRAuthorizationExpired):
        await server.adapter().write_clinical_note(NOTE)


async def test_an_endpoint_that_does_not_accept_the_type_is_not_found() -> None:
    server = CreateServer(httpx.Response(404))

    with pytest.raises(FHIRResourceNotFound):
        await server.adapter().write_clinical_note(NOTE)


async def test_a_server_error_is_the_retryable_one() -> None:
    server = CreateServer(httpx.Response(503))

    with pytest.raises(FHIRUpstreamUnavailable) as raised:
        await server.adapter().write_clinical_note(NOTE)
    assert raised.value.timed_out is False


async def test_a_refused_resource_is_malformed_rather_than_unavailable() -> None:
    """A 422 is our bug or a vendor quirk. Retrying asks the same question again —
    or lands a second document if the first was in fact accepted."""
    server = CreateServer(httpx.Response(422, json={"resourceType": "OperationOutcome"}))

    with pytest.raises(FHIRMalformedResponse):
        await server.adapter().write_clinical_note(NOTE)


async def test_a_timeout_is_reported_as_a_timeout() -> None:
    """Genuinely ambiguous — the EHR may have filed it — so it is never auto-retried."""
    server = CreateServer(httpx.TimeoutException("too slow"))

    with pytest.raises(FHIRUpstreamUnavailable) as raised:
        await server.adapter().write_clinical_note(NOTE)
    assert raised.value.timed_out is True


async def test_the_request_carries_the_token_and_fhir_content_type() -> None:
    """Per request, never as a default header on the shared client."""
    server = CreateServer(created(f"{FHIR_BASE_URL}/DocumentReference/docref-9"))

    await server.adapter().write_clinical_note(NOTE)

    request = server.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{FHIR_BASE_URL}/DocumentReference"
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["content-type"] == "application/fhir+json"


async def test_the_body_uses_fhir_element_names_and_omits_empty_ones() -> None:
    """A server rejects snake_case elements, and nulls are not valid FHIR."""
    server = CreateServer(created(f"{FHIR_BASE_URL}/DocumentReference/docref-9"))

    await server.adapter().write_clinical_note(NOTE)

    body = server.bodies[0]
    assert body["resourceType"] == "DocumentReference"
    assert body["docStatus"] == "preliminary"
    assert "doc_status" not in body
    assert "authenticator" not in body
    assert None not in body.values()


async def test_the_written_bytes_carry_no_machine_suggestion() -> None:
    """The filter asserted at the wire, which is the only place that finally matters."""
    server = CreateServer(created(f"{FHIR_BASE_URL}/DocumentReference/docref-9"))

    await server.adapter().write_clinical_note(NOTE)

    attached = base64.b64decode(server.bodies[0]["content"][0]["attachment"]["data"]).decode()
    assert LLM_CODE.code in attached
    assert SUGGESTED_CODE.code not in attached


async def test_an_unfiltered_note_with_only_suggestions_writes_no_codes() -> None:
    server = CreateServer(created(f"{FHIR_BASE_URL}/DocumentReference/docref-9"))
    suggestion = NoteCode(code="E11.9", source="comprehend-medical")
    note = NOTE.model_copy(update={"icd10_codes": [suggestion]})

    await server.adapter().write_clinical_note(note)

    attached = base64.b64decode(server.bodies[0]["content"][0]["attachment"]["data"]).decode()
    assert "E11.9" not in attached
