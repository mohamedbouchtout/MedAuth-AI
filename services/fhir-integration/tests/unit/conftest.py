"""A fake FHIR server, shared by the adapter tests and the route tests.

Deliberately narrow: it answers the six reads the adapter layer makes and
nothing else. Resource bodies are shaped like what the local HAPI FHIR server
returns for a Synthea patient — camelCase elements, a search ``Bundle`` with an
``entry`` list — because that is the shape the gated integration test will meet
for real.

**These are hand-written approximations, and they are labelled as such here.**
They are not a substitute for the HAPI FHIR test that TASK-052 requires; what
they buy is being able to drive every branch, including ones a live server will
not produce on demand, such as an ``OperationOutcome`` returned with a 200.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

FHIR_BASE_URL = "https://fhir.example-hospital.org/r4"

ACCESS_TOKEN = "ehr-access-token-value"


def patient_resource(
    patient_id: str = "synthea-123", *, address_state: str | None = "MA"
) -> dict[str, Any]:
    """A US Core ``Patient`` with the elements ``PatientInfo`` flattens.

    ``address_state`` is the patient's *residence*. It is never a source for the
    encounter's state — it exists so the disagreement warning can be driven.
    """
    resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": patient_id,
        "name": [{"use": "official", "family": "Sanchez", "given": ["Aurelio", "Luis"]}],
        "gender": "male",
        "birthDate": "1962-04-17",
    }
    if address_state is not None:
        resource["address"] = [
            {"use": "home", "line": ["12 Elm St"], "city": "Leominster", "state": address_state}
        ]
    return resource


def coverage_resource(
    *,
    coverage_id: str = "coverage-1",
    status: str = "active",
    payer_display: str | None = "Aetna Better Health of MA",
    plan_type_text: str | None = "PPO",
    plan_class: str | None = None,
    subscriber_id: str | None = "W123456789",
    order: int | None = None,
) -> dict[str, Any]:
    """A ``Coverage``, with each element the coverage rule reads made optional."""
    resource: dict[str, Any] = {
        "resourceType": "Coverage",
        "id": coverage_id,
        "status": status,
        "beneficiary": {"reference": "Patient/synthea-123"},
        "payor": [{"display": payer_display} if payer_display else {"reference": "Organization/1"}],
    }
    if plan_type_text is not None:
        resource["type"] = {"text": plan_type_text}
    if plan_class is not None:
        resource["class"] = [
            {"type": {"coding": [{"code": "plan"}]}, "value": "PLAN-A", "name": plan_class}
        ]
    if subscriber_id is not None:
        resource["subscriberId"] = subscriber_id
    if order is not None:
        resource["order"] = order
    return resource


def condition_resource(
    *, condition_id: str = "condition-1", clinical_status: str | None = "active"
) -> dict[str, Any]:
    """A ``Condition``, whose ``clinicalStatus`` decides whether it is active."""
    resource: dict[str, Any] = {
        "resourceType": "Condition",
        "id": condition_id,
        "subject": {"reference": "Patient/synthea-123"},
        "code": {"text": "Osteoarthritis of knee"},
    }
    if clinical_status is not None:
        resource["clinicalStatus"] = {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                    "code": clinical_status,
                }
            ]
        }
    return resource


def encounter_resource(
    encounter_id: str = "encounter-1",
    *,
    subject: str | None = "Patient/synthea-123",
    locations: list[dict[str, Any]] | None = None,
    service_provider: str | None = "Organization/org-1",
) -> dict[str, Any]:
    """An ``Encounter`` as the EHR holds it.

    ``locations`` and ``service_provider`` are the two site-of-care sources
    TASK-052b reads ``state`` from, in that order. The default carries both, the
    way a real encounter from HAPI does after ``seed-synthea.sh`` runs.
    """
    resource: dict[str, Any] = {
        "resourceType": "Encounter",
        "id": encounter_id,
        "status": "finished",
    }
    if subject is not None:
        resource["subject"] = {"reference": subject}
    if locations is None:
        locations = [{"location": {"reference": "Location/loc-1"}}]
    if locations:
        resource["location"] = locations
    if service_provider is not None:
        resource["serviceProvider"] = {"reference": service_provider}
    return resource


def location_resource(
    location_id: str = "loc-1",
    *,
    state: str | None = "MA",
    mode: str | None = None,
) -> dict[str, Any]:
    """A ``Location`` — the primary site-of-care source.

    ``address`` is singular here and a list on ``Organization``; that asymmetry
    is R4's, and the resolution code has to handle both.
    """
    resource: dict[str, Any] = {
        "resourceType": "Location",
        "id": location_id,
        "status": "active",
        "name": "HEALTHALLIANCE HOSPITALS, INC",
    }
    if state is not None:
        resource["address"] = {"line": ["60 Hospital Road"], "city": "Leominster", "state": state}
    if mode is not None:
        resource["mode"] = mode
    return resource


def organization_resource(
    organization_id: str = "org-1",
    *,
    addresses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """An ``Organization`` — the fallback site-of-care source."""
    resource: dict[str, Any] = {
        "resourceType": "Organization",
        "id": organization_id,
        "active": True,
        "name": "HEALTHALLIANCE HOSPITALS, INC",
    }
    if addresses is None:
        addresses = [{"line": ["60 Hospital Road"], "city": "Leominster", "state": "MA"}]
    if addresses:
        resource["address"] = addresses
    return resource


def search_bundle(*resources: dict[str, Any]) -> dict[str, Any]:
    """Wrap resources in a searchset ``Bundle``, as a FHIR search returns."""
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": resource} for resource in resources],
    }


class FakeFHIRServer:
    """Answers the reads the adapter makes, with per-path overrides.

    Attributes:
        authorization_headers: Every ``Authorization`` header seen, so a test can
            assert the token is sent per request rather than set on the client.
        requested_paths: Every path requested, for asserting on search params.
    """

    def __init__(self) -> None:
        self.patient: dict[str, Any] | None = patient_resource()
        self.coverages: list[dict[str, Any]] = [coverage_resource()]
        self.conditions: list[dict[str, Any]] = [condition_resource()]
        self.encounter: dict[str, Any] | None = encounter_resource()
        #: Keyed by id so a test can give two locations different states.
        self.locations: dict[str, dict[str, Any]] = {"loc-1": location_resource()}
        self.organizations: dict[str, dict[str, Any]] = {"org-1": organization_resource()}
        #: Path prefix -> a response to return instead of the normal answer.
        self.overrides: dict[str, httpx.Response | Exception] = {}
        self.authorization_headers: list[str | None] = []
        self.requested_paths: list[str] = []
        #: Every resource POSTed to this server, so a test can assert on what was
        #: written rather than only on what came back (TASK-053).
        self.created: list[dict[str, Any]] = []
        #: The id assigned to the next create, returned in a ``Location`` header
        #: the way a real FHIR server answers one.
        self.created_id = "docref-1"

    def fail(self, path_fragment: str, response: httpx.Response | Exception) -> None:
        """Make one resource type answer with a failure instead."""
        self.overrides[path_fragment] = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Route a request to its resource, honouring any override."""
        path = request.url.path
        self.requested_paths.append(str(request.url))
        self.authorization_headers.append(request.headers.get("authorization"))

        for fragment, outcome in self.overrides.items():
            if fragment in path:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        if request.method == "POST":
            self.created.append(json.loads(request.content))
            # 201 with a Location and no body, which is what a conformant server
            # answers a create with by default — ``Prefer: return=representation``
            # is what asks for the resource back, and nothing here sends it.
            return httpx.Response(
                201,
                headers={"Location": f"{FHIR_BASE_URL}{path}/{self.created_id}/_history/1"},
            )

        if "/Patient/" in path:
            if self.patient is None:
                return httpx.Response(404, json={"resourceType": "OperationOutcome"})
            return httpx.Response(200, json=self.patient)
        if path.endswith("/Coverage"):
            return httpx.Response(200, json=search_bundle(*self.coverages))
        if path.endswith("/Condition"):
            return httpx.Response(200, json=search_bundle(*self.conditions))
        if "/Encounter/" in path:
            if self.encounter is None:
                return httpx.Response(404, json={"resourceType": "OperationOutcome"})
            return httpx.Response(200, json=self.encounter)
        for fragment, store in (
            ("/Location/", self.locations),
            ("/Organization/", self.organizations),
        ):
            if fragment in path:
                resource = store.get(path.rsplit("/", 1)[-1])
                if resource is None:
                    return httpx.Response(404, json={"resourceType": "OperationOutcome"})
                return httpx.Response(200, json=resource)

        return httpx.Response(404, json={"resourceType": "OperationOutcome"})

    def client(self) -> httpx.AsyncClient:
        """Return an httpx client whose requests this fake answers."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
