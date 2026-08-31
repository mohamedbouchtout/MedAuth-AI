"""The adapter against a real HAPI FHIR R4 server. TASK-052.

**Why this exists alongside the unit tests.** Those drive every branch against a
hand-written transport, which is the only way to produce a 200
``OperationOutcome`` or a tie on ``Coverage.order`` on demand. What they cannot
check is whether this repository's idea of a FHIR server matches one: real
search semantics, the real shape of a searchset ``Bundle``, real element casing,
and a real 404. That is what a live server is for, and it is the reason CLAUDE.md
keeps parsers honest against real upstream output rather than only fixtures.

**These tests create their own resources rather than reading Synthea's.**
TASK-052 asks for a check against a HAPI server loaded with Synthea patients, and
``scripts/seed-synthea.sh`` is that path — but seeding downloads a ~150MB JAR and
generates a population, which is minutes per run and needs a JDK. Paying that on
every pull request to assert field mapping would be a poor trade, so the split
is: this test posts the handful of resources it needs to a real server and reads
them back through the adapter, and the fuller Synthea check stays a developer and
nightly path. What is verified here is the part that can silently rot — the
mapping — against the server that will actually answer it.

**Skipping is not silent.** With ``REQUIRE_HAPI_TESTS=1`` set, an unreachable
server is a failure rather than a skip. CI sets it, because a test that quietly
skips when its dependency is missing is a test nobody notices has stopped
running — the same reasoning that pairs an env-gated test with a scheduled run
in CLAUDE.md.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from src.adapters.base import EHRAdapter
from src.adapters.errors import FHIRResourceNotFound

HAPI_BASE_URL = os.environ.get("HAPI_FHIR_BASE_URL", "http://localhost:8080/fhir")
REQUIRE_HAPI = os.environ.get("REQUIRE_HAPI_TESTS") == "1"

pytestmark = pytest.mark.integration


def _server_is_up() -> bool:
    try:
        response = httpx.get(f"{HAPI_BASE_URL}/metadata", timeout=10.0)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


@pytest.fixture(scope="module")
def hapi() -> str:
    """The HAPI base URL, or skip — unless CI has said skipping is not allowed."""
    if _server_is_up():
        return HAPI_BASE_URL
    if REQUIRE_HAPI:
        pytest.fail(
            f"REQUIRE_HAPI_TESTS=1 but no FHIR server answered at {HAPI_BASE_URL}. "
            "Start it with 'docker compose up -d --wait hapi-fhir'."
        )
    pytest.skip(f"no HAPI FHIR server at {HAPI_BASE_URL}")


@pytest.fixture(scope="module")
def seeded(hapi: str) -> dict[str, str]:
    """Create one patient with coverage and a condition, and return their ids.

    Ids are unique per run, so repeated runs against a persistent dev server do
    not collide and nothing has to be torn down.
    """
    marker = uuid.uuid4().hex[:12]
    with httpx.Client(base_url=hapi, timeout=30.0) as client:
        patient = client.post(
            "/Patient",
            json={
                "resourceType": "Patient",
                "identifier": [{"system": "urn:medauth:test", "value": marker}],
                "name": [{"use": "official", "family": "Testpatient", "given": ["Ada", "Marie"]}],
                "gender": "female",
                "birthDate": "1971-11-02",
            },
            headers={"Content-Type": "application/fhir+json"},
        )
        patient.raise_for_status()
        patient_id = patient.json()["id"]

        coverage = client.post(
            "/Coverage",
            json={
                "resourceType": "Coverage",
                "status": "active",
                "type": {"text": "PPO"},
                "subscriberId": f"MEM-{marker}",
                "beneficiary": {"reference": f"Patient/{patient_id}"},
                "payor": [{"display": "Blue Cross Blue Shield of Massachusetts"}],
            },
            headers={"Content-Type": "application/fhir+json"},
        )
        coverage.raise_for_status()

        for status, code in (("active", "active"), ("resolved", "resolved")):
            condition = client.post(
                "/Condition",
                json={
                    "resourceType": "Condition",
                    "clinicalStatus": {
                        "coding": [
                            {
                                "system": (
                                    "http://terminology.hl7.org/CodeSystem/condition-clinical"
                                ),
                                "code": code,
                            }
                        ]
                    },
                    "code": {"text": f"Test condition ({status})"},
                    "subject": {"reference": f"Patient/{patient_id}"},
                },
                headers={"Content-Type": "application/fhir+json"},
            )
            condition.raise_for_status()

        encounter = client.post(
            "/Encounter",
            json={
                "resourceType": "Encounter",
                "status": "finished",
                "subject": {"reference": f"Patient/{patient_id}"},
            },
            headers={"Content-Type": "application/fhir+json"},
        )
        encounter.raise_for_status()

    return {"patient_id": patient_id, "encounter_id": encounter.json()["id"]}


@pytest.fixture
async def adapter(hapi: str):  # noqa: ANN201 - an async generator fixture
    """A base adapter pointed at the real server.

    HAPI needs no authorization, so the token is a placeholder — what is under
    test here is the FHIR mapping, not the credential.
    """
    async with httpx.AsyncClient() as client:
        yield EHRAdapter(fhir_base_url=hapi, access_token="unused-by-hapi", http_client=client)


async def test_the_patient_maps_from_a_real_server(
    adapter: EHRAdapter, seeded: dict[str, str]
) -> None:
    patient = await adapter.get_patient(seeded["patient_id"])

    assert patient.patient_id == seeded["patient_id"]
    assert patient.family_name == "Testpatient"
    assert patient.given_names == ["Ada", "Marie"]
    assert patient.birth_date == "1971-11-02"
    assert patient.gender == "female"


async def test_the_coverage_maps_from_a_real_search(
    adapter: EHRAdapter, seeded: dict[str, str]
) -> None:
    """The searchset Bundle a real server returns, not a fixture's idea of one."""
    coverage = await adapter.get_coverage(seeded["patient_id"])

    assert coverage is not None
    assert coverage.payer == "Blue Cross Blue Shield of Massachusetts"
    assert coverage.plan_type == "PPO"
    assert coverage.member_id is not None


async def test_only_active_conditions_come_back(
    adapter: EHRAdapter, seeded: dict[str, str]
) -> None:
    conditions = await adapter.get_conditions(seeded["patient_id"])

    texts = {condition.code.text for condition in conditions if condition.code}
    assert "Test condition (active)" in texts
    assert "Test condition (resolved)" not in texts


async def test_the_encounter_reads_back(adapter: EHRAdapter, seeded: dict[str, str]) -> None:
    encounter = await adapter.get_encounter(seeded["encounter_id"])

    assert encounter.id == seeded["encounter_id"]
    assert encounter.status == "finished"


async def test_the_composed_context_is_fully_populated(
    adapter: EHRAdapter, seeded: dict[str, str]
) -> None:
    """TASK-052's "verify all fields populated", against a real server."""
    context = await adapter.get_patient_context(seeded["patient_id"])

    assert context.patient.family_name == "Testpatient"
    assert context.coverage is not None
    assert context.coverage.payer is not None
    assert context.coverage.plan_type is not None
    assert context.conditions
    assert context.requires_manual_confirmation is False


async def test_a_real_404_is_a_not_found(adapter: EHRAdapter) -> None:
    """What a real server does for a missing resource, not what a fixture says."""
    with pytest.raises(FHIRResourceNotFound):
        await adapter.get_patient(f"no-such-patient-{uuid.uuid4().hex}")


async def test_a_patient_with_no_coverage_is_not_a_not_found(
    adapter: EHRAdapter, hapi: str
) -> None:
    """An empty search Bundle from a real server is an answer, not a 404."""
    async with httpx.AsyncClient(base_url=hapi, timeout=30.0) as client:
        response = await client.post(
            "/Patient",
            json={"resourceType": "Patient", "name": [{"family": "Uninsured"}]},
            headers={"Content-Type": "application/fhir+json"},
        )
        response.raise_for_status()
        patient_id = response.json()["id"]

    assert await adapter.get_coverage(patient_id) is None
