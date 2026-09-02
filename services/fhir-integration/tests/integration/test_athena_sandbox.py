"""The adapter against the real Athenahealth sandbox. TASK-052.

**Gated, and paired with a scheduled run.** ``RUN_ATHENA_LIVE_TESTS`` is off
everywhere except the ``athenahealth-sandbox`` job in
``.github/workflows/nightly-live-checks.yml``, so an unrelated pull request never
goes red because a vendor sandbox was being rebuilt — and the gate is honest
rather than a deletion, because something actually opens it nightly. Standing
rule in CLAUDE.md; the same treatment TASK-013's CMS checks get, with no new
reasoning.

**This test has never passed, and that is recorded rather than hidden.** It is
not known that working sandbox credentials exist, that ``SMART_REDIRECT_URI`` is
registered against them, or that the developer-program access level grants FHIR
reads — the Development-versus-Preview question is unresolved and nothing in this
repository evidences a successful launch against Athena. So the first real run
may fail for access reasons rather than code reasons, and the failure should be
read that way first.

Athenahealth is first in CLAUDE.md's EHR priority order and TASK-055 is where it
is genuinely validated. **TASK-052 must not be closed on the strength of this
file**; what closes TASK-052 is the HAPI check next to it.

Credentials come from the environment and are never written down here. The
access token is obtained by client credentials rather than through a SMART
launch: a launch needs a browser and an EHR user, which a nightly job has
neither of, and what is under test is the FHIR mapping rather than the OAuth
flow — that already has its own tests.
"""

from __future__ import annotations

import os

import httpx
import pytest

from src.adapters import EHRType, get_adapter
from src.adapters.base import EHRAdapter
from src.adapters.models import ClinicalNoteContent, NoteCode

RUN_LIVE = os.environ.get("RUN_ATHENA_LIVE_TESTS") == "1"

CLIENT_ID = os.environ.get("ATHENA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ATHENA_CLIENT_SECRET", "")
FHIR_BASE_URL = os.environ.get(
    "ATHENA_FHIR_BASE_URL", "https://api.preview.platform.athenahealth.com/fhir/r4"
)
TOKEN_URL = os.environ.get(
    "ATHENA_TOKEN_URL", "https://api.preview.platform.athenahealth.com/oauth2/v1/token"
)
TEST_PATIENT_ID = os.environ.get("ATHENA_TEST_PATIENT_ID", "")
#: The encounter a written note is filed against (TASK-053). Separate from the
#: patient because a DocumentReference names both, and neither may be invented.
TEST_ENCOUNTER_ID = os.environ.get("ATHENA_TEST_ENCOUNTER_ID", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not RUN_LIVE,
        reason="Athenahealth sandbox checks are gated on RUN_ATHENA_LIVE_TESTS=1",
    ),
]


@pytest.fixture(scope="module")
def access_token() -> str:
    """Obtain a sandbox access token, or fail naming what is missing.

    A missing credential fails rather than skips: inside this job the gate is
    already on, and skipping here would mean the nightly reports success while
    checking nothing — which is the failure mode the gate-plus-schedule
    arrangement exists to prevent.
    """
    missing = [
        name
        for name, value in (
            ("ATHENA_CLIENT_ID", CLIENT_ID),
            ("ATHENA_CLIENT_SECRET", CLIENT_SECRET),
            ("ATHENA_TEST_PATIENT_ID", TEST_PATIENT_ID),
        )
        if not value
    ]
    if missing:
        pytest.fail(
            "Athenahealth sandbox access is not configured: "
            + ", ".join(missing)
            + ". This is the known blocker on TASK-052's real-sandbox "
            "verification, not a code failure — see the job comment in "
            "nightly-live-checks.yml."
        )

    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            # DocumentReference.write is TASK-053's; a registration granted
            # reads only fails here rather than inside the write test.
            "scope": (
                "system/Patient.read system/Coverage.read "
                "system/Condition.read system/DocumentReference.write"
            ),
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30.0,
    )
    if response.status_code != 200:
        pytest.fail(
            f"Athenahealth token endpoint answered {response.status_code}. If this is "
            "invalid_client, the sandbox registration or its access level is the "
            "problem rather than this code."
        )
    return str(response.json()["access_token"])


@pytest.fixture
async def adapter(access_token: str):  # noqa: ANN201 - an async generator fixture
    """The Athena adapter, selected the same way a real launch selects it."""
    async with httpx.AsyncClient() as client:
        yield get_adapter(EHRType.ATHENA, FHIR_BASE_URL, access_token, client)


async def test_the_patient_reads_from_the_sandbox(adapter: EHRAdapter) -> None:
    """A real vendor's Patient, mapped by the standard US Core path."""
    patient = await adapter.get_patient(TEST_PATIENT_ID)

    assert patient.patient_id
    assert patient.family_name, "Athena returned a Patient with no family name"


async def test_the_context_assembles_against_the_sandbox(adapter: EHRAdapter) -> None:
    """The composed read, which is what a SMART launch actually calls.

    ``requires_manual_confirmation`` is deliberately not asserted either way: a
    sandbox patient may genuinely have no Coverage on file, and that is a real
    answer rather than a defect. What is under test is that the assembly runs
    end to end against a vendor server.
    """
    context = await adapter.get_patient_context(TEST_PATIENT_ID)

    assert context.patient.patient_id
    assert isinstance(context.conditions, list)


async def test_a_note_writes_to_the_sandbox(adapter: EHRAdapter) -> None:
    """TASK-053's write-back against a real vendor, and the one test here that writes.

    **This cannot close TASK-053.** Nothing in this repository has yet produced a
    passing run against Athenahealth — see the module docstring — so a failure
    here should be read as an access question first. The HAPI check in
    ``test_hapi_fhir.py`` is the acceptance gate; genuine Athenahealth validation
    is TASK-055.

    It needs an encounter as well as a patient, because a ``DocumentReference``
    without a ``context.encounter`` would be filed against no visit. That is
    configuration rather than something to invent: a fabricated encounter id
    would file a note against a chart entry that does not exist, which is worse
    than not running.

    The scope is wider than the reads above — this asks for
    ``system/DocumentReference.write`` — so a registration granted read access
    only fails at the token endpoint rather than here.
    """
    if not TEST_ENCOUNTER_ID:
        pytest.fail(
            "ATHENA_TEST_ENCOUNTER_ID is not configured, so there is no chart "
            "entry to file a note against. Same known blocker as the credentials "
            "themselves — see the job comment in nightly-live-checks.yml."
        )

    note = ClinicalNoteContent(
        patient_id=TEST_PATIENT_ID,
        encounter_id=TEST_ENCOUNTER_ID,
        subjective="Synthetic sandbox note written by MedAuth AI's TASK-053 check.",
        assessment="No clinical content. This is an automated conformance check.",
        icd10_codes=[
            NoteCode(code="M17.11", display="Osteoarthritis, right knee", source="llm-extraction")
        ],
    )

    document_id = await adapter.write_clinical_note(note)

    assert document_id, "Athena accepted the document without naming what it created"
