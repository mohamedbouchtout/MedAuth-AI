"""The note review routes: what they return, what they change, and what they refuse.

The three properties worth stating up front, because each of them is a rule from
CLAUDE.md rather than an implementation detail these tests happened to pin:

* A ``GET`` never sets ``reviewed_by_provider``. That column records a
  provider's attestation, and a read that flips it records a page load instead.
* An omitted field and an explicit ``null`` are different requests. A provider
  fixing one sentence of the plan section must not thereby declare that the
  encounter has no diagnoses.
* ``provider_edited`` is the server's to set, and only when content actually
  changed.

Backed by fakes rather than PostgreSQL for the same reason the session route
tests are: this file is about the request/response contract, and it has to run on
a machine with no database.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from track_a_clinical import audit
from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.api.notes import (
    ERROR_CODE_NOTE_NOT_GENERATED,
    ERROR_CODE_SESSION_NOT_FOUND,
)
from track_a_clinical.main import create_app
from track_a_clinical.models import (
    ENCOUNTER_STATUS_COMPLETED,
    ClinicalNote,
    Encounter,
)

GENERATED_AT = datetime.datetime(2026, 8, 18, 12, 45, tzinfo=datetime.UTC)

LLM_CODE = {
    "code": "M17.11",
    "display": "Unilateral primary osteoarthritis, right knee",
    "source": "llm-extraction",
    "confidence": None,
    "validation": None,
}
SUGGESTED_CODE = {
    "code": "E11.9",
    "display": "Type 2 diabetes mellitus without complications",
    "source": "comprehend-medical",
    "confidence": 0.91,
    "validation": None,
}


class FakeSession:
    """Answers the two queries the handlers make, in the order they make them.

    ``_load_encounter_and_note`` selects the encounter and then the note, so a
    two-step queue is enough and keeps the fake from having to parse statements.
    """

    def __init__(self, *, encounter: Encounter | None, note: ClinicalNote | None) -> None:
        self.encounter = encounter
        self.note = note
        self.commits = 0
        self.rollbacks = 0
        self._scalars = 0

    async def scalar(self, _statement: Any) -> Encounter | ClinicalNote | None:
        self._scalars += 1
        return self.encounter if self._scalars == 1 else self.note

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class RecordedAudit:
    """Captures the audit calls the handlers make instead of writing rows."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _session: Any, **fields: Any) -> None:
        self.calls.append(fields)

    @property
    def actions(self) -> list[str]:
        return [call["action"] for call in self.calls]


def make_encounter(session_id: uuid.UUID) -> Encounter:
    """Build a detached completed encounter, the state note review happens in."""
    encounter = Encounter(
        session_id=session_id,
        patient_fhir_id="synthea-placeholder-1",
        provider_id=uuid.uuid4(),
        status=ENCOUNTER_STATUS_COMPLETED,
    )
    encounter.id = uuid.uuid4()
    return encounter


def make_note(
    encounter: Encounter,
    *,
    icd10_codes: list[dict[str, Any]] | None = None,
    cpt_codes: list[dict[str, Any]] | None = None,
) -> ClinicalNote:
    """Build a detached note row as TASK-030's consumer would have written it."""
    note = ClinicalNote(
        encounter_id=encounter.id,
        soap_subjective="Patient reports right knee pain for three months.",
        soap_objective="Tenderness over the medial joint line.",
        soap_assessment="Likely primary osteoarthritis of the right knee.",
        soap_plan="Order MRI right knee. Trial of physical therapy.",
        icd10_codes=icd10_codes,
        cpt_codes=cpt_codes,
    )
    note.id = uuid.uuid4()
    note.generated_at = GENERATED_AT
    note.reviewed_by_provider = False
    note.provider_edited = False
    note.ehr_document_ref_id = None
    return note


@pytest.fixture
def session_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def encounter(session_id: uuid.UUID) -> Encounter:
    return make_encounter(session_id)


@pytest.fixture
def note(encounter: Encounter) -> ClinicalNote:
    return make_note(encounter, icd10_codes=[LLM_CODE], cpt_codes=[])


@pytest.fixture
def recorded_audit(monkeypatch: pytest.MonkeyPatch) -> RecordedAudit:
    recorder = RecordedAudit()
    monkeypatch.setattr(audit, "audit_note_access", recorder)
    return recorder


def build_client(fake: FakeSession) -> AsyncClient:
    """Return a client bound to an app whose database session is `fake`."""
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: fake
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://track-a-clinical")


@pytest_asyncio.fixture
async def fake(encounter: Encounter, note: ClinicalNote) -> FakeSession:
    return FakeSession(encounter=encounter, note=note)


@pytest_asyncio.fixture
async def client(fake: FakeSession, recorded_audit: RecordedAudit) -> AsyncIterator[AsyncClient]:
    async with build_client(fake) as http:
        yield http


async def test_a_stored_note_is_returned_with_its_sections_and_codes(
    client: AsyncClient, session_id: uuid.UUID, note: ClinicalNote
) -> None:
    response = await client.get(f"/notes/{session_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["data"]["session_id"] == str(session_id)
    assert body["data"]["note_id"] == str(note.id)
    assert body["data"]["soap_assessment"] == note.soap_assessment
    assert body["data"]["icd10_codes"] == [LLM_CODE]


async def test_codes_never_determined_stay_null_on_the_wire(
    encounter: Encounter, recorded_audit: RecordedAudit, session_id: uuid.UUID
) -> None:
    """`null` and `[]` are different answers, and a review screen must see which."""
    unanswered = make_note(encounter, icd10_codes=None, cpt_codes=[])
    async with build_client(FakeSession(encounter=encounter, note=unanswered)) as http:
        response = await http.get(f"/notes/{session_id}")

    assert response.json()["data"]["icd10_codes"] is None
    assert response.json()["data"]["cpt_codes"] == []


async def test_reading_a_note_does_not_mark_it_reviewed(
    client: AsyncClient, session_id: uuid.UUID, note: ClinicalNote, fake: FakeSession
) -> None:
    """A read records that someone looked; it never records that they attested."""
    await client.get(f"/notes/{session_id}")

    assert note.reviewed_by_provider is False
    assert note.provider_edited is False
    # One commit, and it carries the audit row alone — the read changed nothing.
    assert fake.commits == 1


async def test_reading_a_note_audits_the_access_with_its_client(
    client: AsyncClient,
    session_id: uuid.UUID,
    note: ClinicalNote,
    encounter: Encounter,
    recorded_audit: RecordedAudit,
) -> None:
    await client.get(f"/notes/{session_id}", headers={"user-agent": "medauth-web/1.0"})

    assert recorded_audit.actions == [audit.ACTION_READ_NOTE]
    call = recorded_audit.calls[0]
    assert call["note_id"] == note.id
    # From the encounter row, never from anything the caller sent — these routes
    # take no credential in v1.
    assert call["provider_id"] == encounter.provider_id
    assert call["user_agent"] == "medauth-web/1.0"


async def test_an_unknown_session_is_not_the_same_as_an_ungenerated_note(
    recorded_audit: RecordedAudit, session_id: uuid.UUID
) -> None:
    async with build_client(FakeSession(encounter=None, note=None)) as http:
        response = await http.get(f"/notes/{session_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ERROR_CODE_SESSION_NOT_FOUND


async def test_an_encounter_whose_note_is_not_written_yet_says_so(
    encounter: Encounter, recorded_audit: RecordedAudit, session_id: uuid.UUID
) -> None:
    """The ordinary state for a few seconds after a visit ends, while Sonnet runs."""
    async with build_client(FakeSession(encounter=encounter, note=None)) as http:
        response = await http.get(f"/notes/{session_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ERROR_CODE_NOTE_NOT_GENERATED


async def test_a_missing_note_audits_nothing(
    encounter: Encounter, recorded_audit: RecordedAudit, session_id: uuid.UUID
) -> None:
    async with build_client(FakeSession(encounter=encounter, note=None)) as http:
        await http.get(f"/notes/{session_id}")

    assert recorded_audit.calls == []


async def test_editing_a_section_sets_provider_edited(
    client: AsyncClient, session_id: uuid.UUID, note: ClinicalNote
) -> None:
    response = await client.patch(
        f"/notes/{session_id}",
        json={"soap_plan": "Order MRI right knee. Defer physical therapy."},
    )

    assert response.status_code == 200
    assert response.json()["data"]["provider_edited"] is True
    assert note.soap_plan == "Order MRI right knee. Defer physical therapy."


async def test_an_omitted_code_list_is_left_alone(
    encounter: Encounter, recorded_audit: RecordedAudit, session_id: uuid.UUID
) -> None:
    """The bug this exists to prevent: a text edit silently clearing the diagnoses.

    The note starts with ``icd10_codes`` as NULL — never determined — which is
    the case a ``None`` default would quietly convert into ``[]``, turning "not
    determined" into "determined to be none".
    """
    unanswered = make_note(encounter, icd10_codes=None, cpt_codes=[LLM_CODE])
    fake = FakeSession(encounter=encounter, note=unanswered)
    async with build_client(fake) as http:
        response = await http.patch(f"/notes/{session_id}", json={"soap_subjective": "Rewritten."})

    assert response.status_code == 200
    assert unanswered.icd10_codes is None
    assert unanswered.cpt_codes == [LLM_CODE]
    assert response.json()["data"]["icd10_codes"] is None


async def test_an_explicit_null_clears_a_code_list(
    client: AsyncClient, session_id: uuid.UUID, note: ClinicalNote
) -> None:
    """The other half of the tri-state: sending null is a real instruction."""
    response = await client.patch(f"/notes/{session_id}", json={"icd10_codes": None})

    assert response.status_code == 200
    assert note.icd10_codes is None
    assert response.json()["data"]["icd10_codes"] is None
    assert response.json()["data"]["provider_edited"] is True


async def test_marking_a_note_reviewed_is_not_an_edit(
    client: AsyncClient, session_id: uuid.UUID, note: ClinicalNote
) -> None:
    response = await client.patch(f"/notes/{session_id}", json={"reviewed_by_provider": True})

    assert response.status_code == 200
    assert response.json()["data"]["reviewed_by_provider"] is True
    assert response.json()["data"]["provider_edited"] is False
    assert note.provider_edited is False


async def test_resending_unchanged_text_is_not_an_edit(
    client: AsyncClient, session_id: uuid.UUID, note: ClinicalNote
) -> None:
    """A client re-sending what it was given has edited nothing, and the flag says so."""
    response = await client.patch(f"/notes/{session_id}", json={"soap_plan": note.soap_plan})

    assert response.status_code == 200
    assert response.json()["data"]["provider_edited"] is False


async def test_accepting_a_suggestion_rewrites_its_source(
    encounter: Encounter, recorded_audit: RecordedAudit, session_id: uuid.UUID
) -> None:
    """How a machine suggestion becomes documentation TASK-060 may claim."""
    suggested = make_note(encounter, icd10_codes=[LLM_CODE, SUGGESTED_CODE], cpt_codes=[])
    accepted = dict(SUGGESTED_CODE, source="provider-accepted", confidence=None)
    async with build_client(FakeSession(encounter=encounter, note=suggested)) as http:
        response = await http.patch(
            f"/notes/{session_id}", json={"icd10_codes": [LLM_CODE, accepted]}
        )

    assert response.status_code == 200
    assert response.json()["data"]["icd10_codes"][1]["source"] == "provider-accepted"
    assert response.json()["data"]["icd10_codes"][1]["confidence"] is None


async def test_an_acceptance_carrying_a_score_is_rejected(
    client: AsyncClient, session_id: uuid.UUID
) -> None:
    """A human acceptance is a fact, not a probability — the score is not forwarded."""
    response = await client.patch(
        f"/notes/{session_id}",
        json={"icd10_codes": [dict(SUGGESTED_CODE, source="provider-accepted")]},
    )

    assert response.status_code == 422


async def test_a_body_that_sets_nothing_is_rejected(
    client: AsyncClient, session_id: uuid.UUID
) -> None:
    """Answering 200 would write an audit row for an edit nobody attempted."""
    response = await client.patch(f"/notes/{session_id}", json={})

    assert response.status_code == 422


async def test_a_server_owned_field_cannot_be_set_by_a_client(
    client: AsyncClient, session_id: uuid.UUID
) -> None:
    response = await client.patch(f"/notes/{session_id}", json={"provider_edited": True})

    assert response.status_code == 422


async def test_a_rejected_body_is_not_echoed_back(
    client: AsyncClient, session_id: uuid.UUID
) -> None:
    """Request bodies here carry clinical content; the handler reports locations only."""
    response = await client.patch(
        f"/notes/{session_id}",
        json={"soap_plan": "Order MRI right knee.", "unknown_field": "Patient is Jane Doe."},
    )

    assert response.status_code == 422
    assert "Jane Doe" not in response.text


async def test_an_edit_audits_as_an_update(
    client: AsyncClient,
    session_id: uuid.UUID,
    encounter: Encounter,
    recorded_audit: RecordedAudit,
) -> None:
    await client.patch(f"/notes/{session_id}", json={"soap_plan": "Revised."})

    assert recorded_audit.actions == [audit.ACTION_UPDATE_NOTE]
    assert recorded_audit.calls[0]["provider_id"] == encounter.provider_id


async def test_an_edit_of_an_unknown_session_is_a_404(
    recorded_audit: RecordedAudit, session_id: uuid.UUID
) -> None:
    async with build_client(FakeSession(encounter=None, note=None)) as http:
        response = await http.patch(f"/notes/{session_id}", json={"soap_plan": "Revised."})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ERROR_CODE_SESSION_NOT_FOUND
    assert recorded_audit.calls == []
