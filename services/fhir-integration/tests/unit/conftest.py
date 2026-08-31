"""A fake FHIR server, shared by the adapter tests and the route tests.

Deliberately narrow: it answers the four reads the adapter layer makes and
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

from typing import Any

import httpx

FHIR_BASE_URL = "https://fhir.example-hospital.org/r4"

ACCESS_TOKEN = "ehr-access-token-value"


def patient_resource(patient_id: str = "synthea-123") -> dict[str, Any]:
    """A US Core ``Patient`` with the elements ``PatientInfo`` flattens."""
    return {
        "resourceType": "Patient",
        "id": patient_id,
        "name": [{"use": "official", "family": "Sanchez", "given": ["Aurelio", "Luis"]}],
        "gender": "male",
        "birthDate": "1962-04-17",
    }


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


def encounter_resource(encounter_id: str = "encounter-1") -> dict[str, Any]:
    """An ``Encounter`` as the EHR holds it."""
    return {
        "resourceType": "Encounter",
        "id": encounter_id,
        "status": "finished",
        "subject": {"reference": "Patient/synthea-123"},
    }


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
        #: Path prefix -> a response to return instead of the normal answer.
        self.overrides: dict[str, httpx.Response | Exception] = {}
        self.authorization_headers: list[str | None] = []
        self.requested_paths: list[str] = []

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

        return httpx.Response(404, json={"resourceType": "OperationOutcome"})

    def client(self) -> httpx.AsyncClient:
        """Return an httpx client whose requests this fake answers."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
