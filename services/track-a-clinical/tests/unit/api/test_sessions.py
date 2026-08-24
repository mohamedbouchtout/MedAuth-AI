"""The session lifecycle routes: envelope, status codes and the idempotency rule."""

from __future__ import annotations

import json
import uuid

import jwt
import pytest
from httpx import AsyncClient

from tests.unit.api.conftest import FakeRedis, FakeSession, RecordedAudit, make_encounter
from track_a_clinical import audit
from track_a_clinical.api.dependencies import get_redis
from track_a_clinical.api.sessions import (
    ERROR_CODE_SESSION_NOT_FOUND,
    ERROR_CODE_SIGNAL_NOT_PUBLISHED,
    SESSIONS_STARTED_CHANNEL,
    session_ended_channel,
)
from track_a_clinical.config import JWT_ALGORITHM
from track_a_clinical.models import ENCOUNTER_STATUS_ACTIVE, ENCOUNTER_STATUS_COMPLETED

START_BODY = {
    "patient_id": "synthea-placeholder-1",
    "provider_id": str(uuid.uuid4()),
    "ehr_encounter_id": "athena-enc-9001",
}


async def test_start_creates_an_encounter_and_returns_a_token(
    client: AsyncClient,
    fake_session: FakeSession,
    signing_key: str,
) -> None:
    response = await client.post("/sessions/start", json=START_BODY)

    assert response.status_code == 201
    body = response.json()
    assert body["error"] is None
    claims = jwt.decode(body["data"]["jwt"], signing_key, algorithms=[JWT_ALGORITHM])
    assert claims["session_id"] == body["data"]["session_id"]
    assert claims["provider_id"] == START_BODY["provider_id"]

    (encounter,) = fake_session.added
    assert str(encounter.session_id) == body["data"]["session_id"]
    assert encounter.status == ENCOUNTER_STATUS_ACTIVE
    assert fake_session.commits == 1


async def test_start_maps_patient_id_onto_the_fhir_column(
    client: AsyncClient, fake_session: FakeSession
) -> None:
    """The wire name and the column name differ on purpose — see CLAUDE.md."""
    await client.post("/sessions/start", json=START_BODY)

    (encounter,) = fake_session.added
    assert encounter.patient_fhir_id == START_BODY["patient_id"]


async def test_start_generates_the_session_id_server_side(
    client: AsyncClient, fake_session: FakeSession
) -> None:
    """A client-supplied session_id must be rejected, not honoured."""
    client_chosen = str(uuid.uuid4())

    response = await client.post(
        "/sessions/start", json={**START_BODY, "session_id": client_chosen}
    )

    assert response.status_code == 422
    assert fake_session.added == []


async def test_start_audits_the_phi_access(
    client: AsyncClient, recorded_audit: RecordedAudit
) -> None:
    response = await client.post("/sessions/start", json=START_BODY)

    assert recorded_audit.actions == [audit.ACTION_START_SESSION]
    call = recorded_audit.calls[0]
    assert call["encounter_id"] is not None
    assert str(call["session_id"]) == response.json()["data"]["session_id"]
    assert str(call["provider_id"]) == START_BODY["provider_id"]


async def test_start_announces_the_new_session(client: AsyncClient, fake_redis: FakeRedis) -> None:
    """TASK-021's consumer cannot subscribe to a session it has not been told about."""
    response = await client.post("/sessions/start", json=START_BODY)

    (channel, payload) = fake_redis.published[0]
    assert channel == SESSIONS_STARTED_CHANNEL
    # The channel is fixed, so unlike the end signal the id has to be in the body.
    assert json.loads(payload) == {"session_id": response.json()["data"]["session_id"]}


async def test_start_reports_a_failed_announcement_instead_of_swallowing_it(
    client: AsyncClient,
) -> None:
    """A session nobody is listening to would raise no nudges and look normal."""
    failing = FakeRedis(fail=True)
    client._transport.app.dependency_overrides[get_redis] = lambda: failing  # type: ignore[attr-defined]

    response = await client.post("/sessions/start", json=START_BODY)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ERROR_CODE_SIGNAL_NOT_PUBLISHED


async def test_validation_failure_uses_the_error_envelope(client: AsyncClient) -> None:
    """FastAPI's default {"detail": ...} shape must not escape."""
    response = await client.post("/sessions/start", json={"patient_id": ""})

    assert response.status_code == 422
    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == "validation_error"
    assert "provider_id" in body["error"]["message"]


async def test_validation_message_does_not_echo_the_patient_identifier(
    client: AsyncClient,
) -> None:
    """A rejected body carries PHI; only field locations may be reported back."""
    response = await client.post(
        "/sessions/start", json={"patient_id": "patient-abc-123", "provider_id": "not-a-uuid"}
    )

    assert response.status_code == 422
    assert "patient-abc-123" not in response.text


async def test_end_completes_the_encounter_and_publishes_the_signal(
    client: AsyncClient,
    fake_session: FakeSession,
    fake_redis: FakeRedis,
    recorded_audit: RecordedAudit,
) -> None:
    session_id = uuid.uuid4()
    fake_session.existing = make_encounter(session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE)

    response = await client.post(f"/sessions/{session_id}/end")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["status"] == ENCOUNTER_STATUS_COMPLETED
    assert body["data"]["already_ended"] is False
    assert body["data"]["ended_at"].startswith("2026-08-18T12:30:00")
    assert fake_redis.published == [(session_ended_channel(session_id), "")]
    assert recorded_audit.actions == [audit.ACTION_END_SESSION]


async def test_end_is_idempotent_and_does_not_republish(
    client: AsyncClient,
    fake_session: FakeSession,
    fake_redis: FakeRedis,
    recorded_audit: RecordedAudit,
) -> None:
    """TASK-030 and TASK-060 both act on the signal — a second one double-fires."""
    session_id = uuid.uuid4()
    fake_session.existing = make_encounter(session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE)

    first = await client.post(f"/sessions/{session_id}/end")
    second = await client.post(f"/sessions/{session_id}/end")

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["data"]["already_ended"] is False
    assert second.json()["data"]["already_ended"] is True
    assert len(fake_redis.published) == 1
    # The repeat still touched the row, so it is still an auditable PHI read.
    assert recorded_audit.actions == [audit.ACTION_END_SESSION, audit.ACTION_READ_ENCOUNTER]


async def test_end_returns_404_for_an_unknown_session(client: AsyncClient) -> None:
    response = await client.post(f"/sessions/{uuid.uuid4()}/end")

    assert response.status_code == 404
    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == ERROR_CODE_SESSION_NOT_FOUND


async def test_end_rejects_a_malformed_session_id(client: AsyncClient) -> None:
    response = await client.post("/sessions/not-a-uuid/end")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_end_reports_a_failed_publish_instead_of_swallowing_it(
    client: AsyncClient, fake_session: FakeSession
) -> None:
    """The row is already committed; a lost signal has to be visible."""
    session_id = uuid.uuid4()
    fake_session.existing = make_encounter(session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE)
    failing = FakeRedis(fail=True)
    client._transport.app.dependency_overrides[get_redis] = lambda: failing  # type: ignore[attr-defined]

    response = await client.post(f"/sessions/{session_id}/end")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ERROR_CODE_SIGNAL_NOT_PUBLISHED


@pytest.mark.parametrize("path", ["/sessions/start", f"/sessions/{uuid.uuid4()}/end"])
async def test_routes_reject_the_wrong_method(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "http_error"
