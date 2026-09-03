"""The adapter against a real HAPI FHIR R4 server. TASK-052, TASK-052b, TASK-053, TASK-054.

**Why this exists alongside the unit tests.** Those drive every branch against a
hand-written transport, which is the only way to produce a 200
``OperationOutcome`` or a tie on ``Coverage.order`` on demand. What they cannot
check is whether this repository's idea of a FHIR server matches one: real
search semantics, the real shape of a searchset ``Bundle``, real element casing,
and a real 404. That is what a live server is for, and it is the reason CLAUDE.md
keeps parsers honest against real upstream output rather than only fixtures.

**These tests create their own resources rather than reading Synthea's, and
for TASK-052b they have to.** TASK-052 asked for a check against a HAPI server
loaded with Synthea patients, and ``scripts/seed-synthea.sh`` is that path — but
seeding downloads a ~150MB JAR and generates a population, which is minutes per
run and needs a JDK. Paying that on every pull request to assert field mapping
would be a poor trade.

For the payer columns the reason is stronger than cost: **Synthea emits no
searchable ``Coverage`` resource at all.** It builds one and calls
``eob.addContained(coverage)``, under an open ``// TODO: Make Coverage separate
resources for US Core 6 & 7?`` in ``FhirR4.java``; across 40 real sample bundles
containing 2,348 encounters the count of standalone ``Coverage`` resources is
zero. So ``Coverage?patient=`` against a Synthea-seeded server comes back empty
and a Synthea patient can only ever exercise the NULL path. Synthea also
generates a single-state population, so it cannot exercise the out-of-state
disagreement case either. Both need hand-posted resources, which is what the
fixture below builds.

**Skipping is not silent.** With ``REQUIRE_HAPI_TESTS=1`` set, an unreachable
server is a failure rather than a skip. CI sets it, because a test that quietly
skips when its dependency is missing is a test nobody notices has stopped
running — the same reasoning that pairs an env-gated test with a scheduled run
in CLAUDE.md.
"""

from __future__ import annotations

import base64
import logging
import os
import uuid

import httpx
import pytest

from src.adapters.base import EHRAdapter
from src.adapters.errors import FHIRMalformedResponse, FHIRResourceNotFound
from src.adapters.models import (
    ClinicalNoteContent,
    CoverageInfo,
    NoteCode,
    PriorAuthContent,
    PriorAuthEvidence,
    PriorAuthProcedure,
)
from src.adapters.pas_bundle import build_request_bundle
from src.adapters.site_of_care import organization_state

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
                # Deliberately a different state from the site of care below, so
                # the disagreement path runs against real resources rather than
                # only against a fixture. A residence is never the cache key's
                # state; see src/adapters/site_of_care.py.
                "address": [{"use": "home", "city": "Nashua", "state": "NH"}],
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

        organization = client.post(
            "/Organization",
            json={
                "resourceType": "Organization",
                "active": True,
                "name": f"Testclinic {marker}",
                # A billing address first, in a third state, so the real server
                # proves the eligibility rule rather than the fixture doing it.
                "address": [
                    {"use": "billing", "city": "Wilmington", "state": "DE"},
                    {"city": "Leominster", "state": "MA"},
                ],
            },
            headers={"Content-Type": "application/fhir+json"},
        )
        organization.raise_for_status()
        organization_id = organization.json()["id"]

        location = client.post(
            "/Location",
            json={
                "resourceType": "Location",
                "status": "active",
                "name": f"Testclinic room {marker}",
                "address": {"city": "Leominster", "state": "MA"},
                "managingOrganization": {"reference": f"Organization/{organization_id}"},
            },
            headers={"Content-Type": "application/fhir+json"},
        )
        location.raise_for_status()
        location_id = location.json()["id"]

        encounter = client.post(
            "/Encounter",
            json={
                "resourceType": "Encounter",
                "status": "finished",
                "subject": {"reference": f"Patient/{patient_id}"},
                "location": [{"location": {"reference": f"Location/{location_id}"}}],
                "serviceProvider": {"reference": f"Organization/{organization_id}"},
            },
            headers={"Content-Type": "application/fhir+json"},
        )
        encounter.raise_for_status()

        # An encounter with no site of care at all, for the NULL case. A real
        # server is what proves the adapter answers None rather than raising.
        placeless = client.post(
            "/Encounter",
            json={
                "resourceType": "Encounter",
                "status": "finished",
                "subject": {"reference": f"Patient/{patient_id}"},
            },
            headers={"Content-Type": "application/fhir+json"},
        )
        placeless.raise_for_status()

    return {
        "patient_id": patient_id,
        "encounter_id": encounter.json()["id"],
        "placeless_encounter_id": placeless.json()["id"],
        "location_id": location_id,
        "organization_id": organization_id,
    }


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
    # The residence, for the disagreement check only — never the cache key.
    assert patient.address_state == "NH"


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


async def test_the_location_and_organization_read_back(
    adapter: EHRAdapter, seeded: dict[str, str]
) -> None:
    """The two primitives TASK-052b added, against the server that will answer them."""
    location = await adapter.get_location(seeded["location_id"])
    organization = await adapter.get_organization(seeded["organization_id"])

    assert location.id == seeded["location_id"]
    assert location.address is not None
    assert location.address.state == "MA"
    assert organization.id == seeded["organization_id"]
    assert organization.address is not None


async def test_the_encounter_coverage_context_populates_all_three_columns(
    adapter: EHRAdapter, seeded: dict[str, str]
) -> None:
    """TASK-052b's first acceptance criterion, against a real FHIR server.

    Against hand-posted resources rather than a Synthea patient — see the module
    docstring for why a Synthea patient cannot satisfy this at all.
    """
    context = await adapter.get_encounter_coverage_context(seeded["encounter_id"])

    assert context.patient_id == seeded["patient_id"]
    assert context.coverage is not None
    assert context.coverage.payer == "Blue Cross Blue Shield of Massachusetts"
    assert context.coverage.plan_type == "PPO"
    assert context.state == "MA"
    assert context.requires_manual_confirmation is False


async def test_the_state_is_the_site_of_care_and_not_the_patients_residence(
    adapter: EHRAdapter, seeded: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """The patient lives in NH and was seen in MA. The answer is MA, and it is logged.

    Asserted against a real server because this is the one rule a later change is
    most likely to "fix" by reaching for ``Patient.address`` — which resolves to
    a plausible value nearly every time and is wrong.
    """
    with caplog.at_level(logging.WARNING):
        context = await adapter.get_encounter_coverage_context(seeded["encounter_id"])

    assert context.state == "MA"
    assert "different states" in caplog.text


async def test_an_encounter_with_no_site_of_care_leaves_the_state_null(
    adapter: EHRAdapter, seeded: dict[str, str]
) -> None:
    """No Location, no serviceProvider — and still no fallback to the residence.

    The patient on this encounter has an NH address. A NULL state is the honest
    record; ``resolve_query_parameters()`` names it as missing, and nothing
    downstream is misled about where the service happened.
    """
    context = await adapter.get_encounter_coverage_context(seeded["placeless_encounter_id"])

    assert context.state is None
    assert context.coverage is not None


async def test_a_billing_address_is_not_read_as_a_site_of_care(
    adapter: EHRAdapter, seeded: dict[str, str]
) -> None:
    """The seeded Organization lists a DE billing address before its MA one.

    A lockbox in another state is ordinary, and reading it would key the cache on
    a state the service did not happen in.
    """
    organization = await adapter.get_organization(seeded["organization_id"])

    assert organization_state(organization) == "MA"


# -- TASK-053: the note write-back, against a real FHIR server ----------------
#
# This is TASK-053's acceptance gate. The unit tests assert the composition and
# the failure mapping against fakes; only this one proves a real FHIR server
# accepts the resource we build — which is the half a fake cannot answer, since
# a fake accepts whatever it is sent.


def write_back_note(**overrides: object) -> ClinicalNoteContent:
    """A note as ``POST /fhir/notes`` would assemble it, with one code of each source."""
    fields: dict[str, object] = {
        "patient_id": "replaced-by-the-test",
        "encounter_id": "replaced-by-the-test",
        "subjective": "Right knee pain for three months, worse on stairs.",
        "objective": "Tenderness over the medial joint line. No effusion.",
        "assessment": "Likely primary osteoarthritis of the right knee.",
        "plan": "Order MRI right knee. Six weeks of physical therapy.",
        "icd10_codes": [
            NoteCode(
                code="M17.11",
                display="Unilateral primary osteoarthritis, right knee",
                source="llm-extraction",
            ),
            NoteCode(
                code="E11.9",
                display="Type 2 diabetes mellitus without complications",
                source="comprehend-medical",
            ),
        ],
        "reviewed_by_provider": False,
    }
    return ClinicalNoteContent(**(fields | overrides))  # type: ignore[arg-type]


async def test_a_note_is_written_to_a_real_server_and_reads_back(
    adapter: EHRAdapter, seeded: dict[str, str], hapi: str
) -> None:
    """The acceptance gate: file a note, then read the document back off the server.

    Asserted on what the server stored rather than on what was sent, so a
    resource HAPI silently rejected or altered cannot pass.
    """
    note = write_back_note(patient_id=seeded["patient_id"], encounter_id=seeded["encounter_id"])

    document_id = await adapter.write_clinical_note(note)

    async with httpx.AsyncClient(base_url=hapi, timeout=30.0) as client:
        response = await client.get(f"/DocumentReference/{document_id}")
        response.raise_for_status()
        stored = response.json()

    assert stored["resourceType"] == "DocumentReference"
    assert stored["status"] == "current"
    assert stored["subject"]["reference"] == f"Patient/{seeded['patient_id']}"
    assert stored["context"]["encounter"][0]["reference"] == f"Encounter/{seeded['encounter_id']}"

    coding = stored["type"]["coding"][0]
    assert (coding["system"], coding["code"]) == ("http://loinc.org", "11506-3")
    assert stored["category"][0]["coding"][0]["code"] == "clinical-note"

    body = base64.b64decode(stored["content"][0]["attachment"]["data"]).decode("utf-8")
    for section in ("Subjective", "Objective", "Assessment", "Plan"):
        assert section in body
    assert "worse on stairs" in body


async def test_a_machine_suggestion_is_absent_from_the_stored_document(
    adapter: EHRAdapter, seeded: dict[str, str], hapi: str
) -> None:
    """The filter, proved where it finally matters: the server's own copy.

    A ``comprehend-medical`` code is one no provider stated. This asserts it is
    not on the chart after a real round trip, not merely that a function
    filtered it on the way past.
    """
    note = write_back_note(patient_id=seeded["patient_id"], encounter_id=seeded["encounter_id"])

    document_id = await adapter.write_clinical_note(note)

    async with httpx.AsyncClient(base_url=hapi, timeout=30.0) as client:
        stored = (await client.get(f"/DocumentReference/{document_id}")).json()
    body = base64.b64decode(stored["content"][0]["attachment"]["data"]).decode("utf-8")

    assert "M17.11" in body
    assert "E11.9" not in body
    assert "diabetes" not in body.lower()


async def test_an_unreviewed_note_is_stored_as_preliminary(
    adapter: EHRAdapter, seeded: dict[str, str], hapi: str
) -> None:
    """And a reviewed one as final — the distinction a chart reader depends on."""
    ids = {"patient_id": seeded["patient_id"], "encounter_id": seeded["encounter_id"]}

    async with httpx.AsyncClient(base_url=hapi, timeout=30.0) as client:
        for reviewed, expected in ((False, "preliminary"), (True, "final")):
            document_id = await adapter.write_clinical_note(
                write_back_note(reviewed_by_provider=reviewed, **ids)
            )
            stored = (await client.get(f"/DocumentReference/{document_id}")).json()

            assert stored["docStatus"] == expected
            assert "authenticator" not in stored


# -- The prior-authorization submission (TASK-054) ----------------------------
#
# **HAPI is not a payer and does not implement ``Claim/$submit``.** TASK-054 asks
# for a check against "local HAPI FHIR as mock payer", and the honest version of
# that is these two tests rather than a round trip that cannot happen: the
# operation is a Da Vinci PAS profile that no general-purpose FHIR server
# provides, and standing up a PAS reference implementation is TASK-054b's
# neighbourhood, not this task's.
#
# What a real server *can* answer is worth more than a fixture in both cases —
# whether the bundle we build is valid FHIR R4 at all, judged by a real
# validator, and what a real 404 on an unimplemented operation maps to.


def submittable_content(patient_id: str, encounter_id: str) -> PriorAuthContent:
    """A request with everything a conformant PAS bundle requires."""
    return PriorAuthContent(
        request_id=str(uuid.uuid4()),
        patient_id=patient_id,
        encounter_id=encounter_id,
        provider_reference=f"{HAPI_BASE_URL}/Practitioner/example",
        payer_name="Aetna",
        coverage=CoverageInfo(payer="Aetna", plan_type="PPO", member_id="W123456789"),
        procedures=[PriorAuthProcedure(cpt_code="27447", description="total knee replacement")],
        icd10_codes=[
            NoteCode(
                code="M17.11",
                display="Unilateral primary osteoarthritis, right knee",
                source="llm-extraction",
            )
        ],
        clinical_evidence=[
            PriorAuthEvidence(text="12 weeks of physical therapy with no improvement")
        ],
    )


async def test_the_request_bundle_is_valid_fhir_by_a_real_validator(
    seeded: dict[str, str], hapi: str
) -> None:
    """Post the bundle to ``Bundle/$validate`` and require no error or fatal issue.

    This is what a real server can tell us that a fixture cannot: whether the
    resource we compose is well-formed R4 at all. A cardinality mistake or a
    misspelled element that every unit test here would happily round-trip is
    caught by a validator that has the actual StructureDefinitions.

    ``warning`` and ``information`` issues are allowed through: HAPI warns about
    unresolvable references, and every reference in a PAS request bundle is
    either relative to the payer's server or a ``urn:uuid`` naming a bundle
    entry — so treating a warning as a failure would fail on the shape the
    profile asks for.
    """
    bundle = build_request_bundle(submittable_content(seeded["patient_id"], seeded["encounter_id"]))

    async with httpx.AsyncClient(base_url=hapi, timeout=30.0) as client:
        response = await client.post(
            "/Bundle/$validate",
            json=bundle.model_dump(by_alias=True, exclude_none=True),
            headers={"Content-Type": "application/fhir+json"},
        )

    outcome = response.json()
    assert outcome["resourceType"] == "OperationOutcome"
    blocking = [
        issue for issue in outcome.get("issue", []) if issue.get("severity") in {"error", "fatal"}
    ]
    assert blocking == [], blocking


async def test_a_server_without_the_pas_operation_fails_loudly(
    adapter: EHRAdapter, seeded: dict[str, str]
) -> None:
    """A server that does not implement ``Claim/$submit`` must not look like a success.

    **HAPI answers 400, not 404**, with an ``OperationOutcome`` whose issue code
    is ``not-supported`` — "The FHIR endpoint on this server does not know how to
    handle POST operation[Claim/$submit]". This test was written expecting a 404
    and the real server said otherwise, which is the whole reason it runs against
    one.

    The 400 maps to ``FHIRMalformedResponse`` by ``_invoke``'s rule that a 4xx
    which is not 401/403/404 is our request being refused rather than an outage —
    and that reading is right here: the fix is to submit through another path for
    this payer, never to retry the same request. What matters for a foreseeable
    production state — a payer endpoint that does not speak PAS, which is the
    whole reason ``AthenaAdapter`` overrides the submission — is that it raises
    rather than quietly recording a submission that never happened.
    """
    content = submittable_content(seeded["patient_id"], seeded["encounter_id"])

    with pytest.raises(FHIRMalformedResponse):
        await adapter.submit_prior_auth(content)
