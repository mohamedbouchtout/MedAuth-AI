"""The note's EHR-linkage sub-resource: what it hands a write-back, and what it refuses.

TASK-053. Two properties here are rules rather than implementation details, and
each has its own test below:

* **A note is filed to a chart once.** A second attempt is refused, because two
  ``DocumentReference`` resources for one encounter is duplicate clinical
  documentation — a clinician reading one version while another is amended.
* **The refusal is decided by the update, not by a read before it.** A
  check-then-write has a window in which two callers both pass the check, and
  the harm it is guarding against is on a patient's chart.

Backed by fakes rather than PostgreSQL, like the note review tests next to it:
this file is about the request/response contract. The conditional update's real
behaviour under concurrency is exercised against a real database in
``tests/integration/test_notes_store.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Update

from hipaa_logger import AuditAction
from track_a_clinical import audit
from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.api.notes import (
    ERROR_CODE_NOTE_ALREADY_WRITTEN,
    ERROR_CODE_NOTE_NOT_GENERATED,
    ERROR_CODE_SESSION_NOT_FOUND,
)
from track_a_clinical.main import create_app
from track_a_clinical.models import ClinicalNote, Encounter

from .test_notes import RecordedAudit, make_encounter, make_note

DOCUMENT_ID = "DocumentReference-9f13"


class LinkageSession:
    """A session that models the conditional update rather than always succeeding.

    The selects are answered by looking at what they select rather than by
    counting calls, because one client makes several requests through one fake
    and a counter would drift between them. The ``UPDATE ... RETURNING`` answers
    the way PostgreSQL would: an id when the column was still NULL, nothing when
    another writer had already filled it. A fake that always returned an id would
    let the write-once rule pass by coincidence.
    """

    def __init__(self, *, encounter: Encounter | None, note: ClinicalNote | None) -> None:
        self.encounter = encounter
        self.note = note
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0
        self.updates = 0

    async def scalar(self, statement: Any) -> Any:
        if isinstance(statement, Update):
            return self._apply_update(statement)
        entity = statement.column_descriptions[0]["entity"]
        return self.encounter if entity is Encounter else self.note

    def _apply_update(self, statement: Update) -> uuid.UUID | None:
        self.updates += 1
        if self.note is None or self.note.ehr_document_ref_id is not None:
            return None
        # Stand in for the row the real UPDATE would have written, taking the
        # value from the statement rather than from this module's constant — a
        # fake that wrote what it expected could not fail on a handler that sent
        # something else.
        self.note.ehr_document_ref_id = statement.compile().params["ehr_document_ref_id"]
        return self.note.id

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, _instance: Any) -> None:
        self.refreshes += 1


@pytest.fixture
def session_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def encounter(session_id: uuid.UUID) -> Encounter:
    encounter = make_encounter(session_id)
    encounter.ehr_encounter_id = "Encounter-4471"
    return encounter


@pytest.fixture
def note(encounter: Encounter) -> ClinicalNote:
    return make_note(encounter, icd10_codes=[], cpt_codes=[])


@pytest.fixture
def recorded_audit(monkeypatch: pytest.MonkeyPatch) -> RecordedAudit:
    note_recorder = RecordedAudit()
    monkeypatch.setattr(audit, "audit_note_access", note_recorder)
    monkeypatch.setattr(audit, "audit_encounter_access", note_recorder)
    return note_recorder


@pytest_asyncio.fixture
async def fake(encounter: Encounter, note: ClinicalNote) -> LinkageSession:
    return LinkageSession(encounter=encounter, note=note)


@pytest_asyncio.fixture
async def client(fake: LinkageSession, recorded_audit: RecordedAudit) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: fake
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://track-a-clinical"
    ) as http:
        yield http


async def test_read_returns_the_identifiers_a_write_back_needs(
    client: AsyncClient, session_id: uuid.UUID, encounter: Encounter
) -> None:
    """The chart entry, the subject, and whether a document already exists."""
    response = await client.get(f"/notes/{session_id}/ehr-reference")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {
        "session_id": str(session_id),
        "ehr_encounter_id": encounter.ehr_encounter_id,
        "patient_fhir_id": encounter.patient_fhir_id,
        "ehr_document_ref_id": None,
    }


async def test_read_audits_as_an_encounter_read(
    client: AsyncClient, session_id: uuid.UUID, recorded_audit: RecordedAudit
) -> None:
    """It returns a patient identifier, so it is a PHI read like any other."""
    await client.get(f"/notes/{session_id}/ehr-reference")

    assert recorded_audit.actions == [AuditAction.READ_ENCOUNTER]


async def test_read_reports_a_missing_chart_link_rather_than_hiding_it(
    client: AsyncClient, session_id: uuid.UUID, encounter: Encounter
) -> None:
    """A visit started outside a SMART launch has no chart entry, and says so.

    Null here is an answer, not an error: the caller decides what to do about it,
    and TASK-053's answer is to refuse rather than address a guessed encounter.
    """
    encounter.ehr_encounter_id = None

    response = await client.get(f"/notes/{session_id}/ehr-reference")

    assert response.status_code == 200
    assert response.json()["data"]["ehr_encounter_id"] is None


async def test_recording_stores_the_document_id(
    client: AsyncClient, session_id: uuid.UUID, note: ClinicalNote
) -> None:
    response = await client.patch(
        f"/notes/{session_id}/ehr-reference",
        json={"ehr_document_ref_id": DOCUMENT_ID},
    )

    assert response.status_code == 200
    assert response.json()["data"]["ehr_document_ref_id"] == DOCUMENT_ID
    assert note.ehr_document_ref_id == DOCUMENT_ID


async def test_recording_audits_as_a_write_to_the_ehr(
    client: AsyncClient, session_id: uuid.UUID, recorded_audit: RecordedAudit
) -> None:
    """Its own action, not WRITE_NOTE — that one means the note was generated here."""
    await client.patch(
        f"/notes/{session_id}/ehr-reference",
        json={"ehr_document_ref_id": DOCUMENT_ID},
    )

    assert recorded_audit.actions == [AuditAction.WRITE_NOTE_TO_EHR]


async def test_recording_takes_its_actor_from_the_encounter(
    client: AsyncClient, session_id: uuid.UUID, encounter: Encounter, recorded_audit: RecordedAudit
) -> None:
    """A service-to-service hop does not change whose visit this is."""
    await client.patch(
        f"/notes/{session_id}/ehr-reference",
        json={"ehr_document_ref_id": DOCUMENT_ID},
    )

    assert recorded_audit.calls[0]["provider_id"] == encounter.provider_id


async def test_a_second_recording_is_refused(
    client: AsyncClient, session_id: uuid.UUID, note: ClinicalNote, fake: LinkageSession
) -> None:
    """Write-once. The second attempt changes nothing and answers 409."""
    first = await client.patch(
        f"/notes/{session_id}/ehr-reference",
        json={"ehr_document_ref_id": DOCUMENT_ID},
    )
    assert first.status_code == 200

    second = await client.patch(
        f"/notes/{session_id}/ehr-reference",
        json={"ehr_document_ref_id": "DocumentReference-second-attempt"},
    )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == ERROR_CODE_NOTE_ALREADY_WRITTEN
    assert note.ehr_document_ref_id == DOCUMENT_ID
    assert fake.rollbacks == 1


async def test_a_refused_recording_writes_no_audit_row(
    client: AsyncClient, session_id: uuid.UUID, recorded_audit: RecordedAudit
) -> None:
    """Nothing was written, so a row claiming a note was filed would be a lie."""
    await client.patch(
        f"/notes/{session_id}/ehr-reference",
        json={"ehr_document_ref_id": DOCUMENT_ID},
    )
    recorded_audit.calls.clear()

    await client.patch(
        f"/notes/{session_id}/ehr-reference",
        json={"ehr_document_ref_id": "DocumentReference-second-attempt"},
    )

    assert recorded_audit.calls == []


async def test_an_empty_body_is_refused(client: AsyncClient, session_id: uuid.UUID) -> None:
    """The transition is named explicitly; an empty PATCH names nothing."""
    response = await client.patch(f"/notes/{session_id}/ehr-reference", json={})

    assert response.status_code == 422


async def test_unknown_session_and_ungenerated_note_stay_distinct(
    session_id: uuid.UUID, encounter: Encounter, recorded_audit: RecordedAudit
) -> None:
    """The same two 404s the review routes make, for the same reason."""
    for fake, expected in (
        (LinkageSession(encounter=None, note=None), ERROR_CODE_SESSION_NOT_FOUND),
        (LinkageSession(encounter=encounter, note=None), ERROR_CODE_NOTE_NOT_GENERATED),
    ):
        app = create_app()
        app.dependency_overrides[get_db_session] = lambda fake=fake: fake  # type: ignore[misc]
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://track-a-clinical"
        ) as http:
            response = await http.get(f"/notes/{session_id}/ehr-reference")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == expected
