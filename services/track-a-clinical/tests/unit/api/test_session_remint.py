"""``POST /sessions/{session_id}/token`` — the re-mint route (TASK-006b).

The four behaviours TASKS.md asks for are here: same session and provider with a
later expiry, no new encounter row, 409 on a completed session, 404 on an unknown
or soft-deleted one. The fifth — that the token is accepted by TASK-020's real
validator — cannot live here, because ``import src.auth`` in this service's
environment resolves to whichever service sorts first among the four that still
install a top-level ``src``. It lives in
``services/audio-ingestion/tests/unit/test_remint_token_contract.py`` instead,
where that import is unambiguous.
"""

from __future__ import annotations

import datetime
import uuid

import jwt
import pytest
from httpx import AsyncClient

from tests.unit.api.conftest import FakeRedis, FakeSession, RecordedAudit, make_encounter
from track_a_clinical import audit
from track_a_clinical.api.sessions import (
    ERROR_CODE_AUTH_REJECTED,
    ERROR_CODE_SESSION_COMPLETED,
    ERROR_CODE_SESSION_NOT_FOUND,
)
from track_a_clinical.config import JWT_ALGORITHM, get_settings
from track_a_clinical.models import ENCOUNTER_STATUS_ACTIVE, ENCOUNTER_STATUS_COMPLETED
from track_a_clinical.session_tokens import mint_session_jwt


def credential(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None = None,
    key: str,
    expires_at: datetime.datetime | None = None,
) -> dict[str, str]:
    """Build the Authorization header a client would present."""
    body: dict[str, object] = {
        "session_id": str(session_id),
        "provider_id": str(provider_id or uuid.uuid4()),
        "exp": int(
            (
                expires_at or datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
            ).timestamp()
        ),
    }
    return {"Authorization": f"Bearer {jwt.encode(body, key, algorithm=JWT_ALGORITHM)}"}


async def test_remint_returns_a_fresh_token_for_the_same_session_and_provider(
    client: AsyncClient, fake_session: FakeSession, signing_key: str
) -> None:
    """The behaviour the whole task exists for."""
    session_id = uuid.uuid4()
    encounter = make_encounter(session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE)
    fake_session.existing = encounter
    # Issued 20 minutes ago, so it is genuinely past its 15-minute exp — the
    # situation a provider whose visit ran long actually arrives in.
    original = mint_session_jwt(
        session_id=session_id,
        provider_id=encounter.provider_id,
        settings=get_settings(),
        now=datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=20),
    )

    response = await client.post(
        f"/sessions/{session_id}/token",
        headers={"Authorization": f"Bearer {original}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["error"] is None
    assert body["data"]["session_id"] == str(session_id)

    fresh = jwt.decode(body["data"]["jwt"], signing_key, algorithms=[JWT_ALGORITHM])
    stale = jwt.decode(
        original, signing_key, algorithms=[JWT_ALGORITHM], options={"verify_exp": False}
    )
    assert fresh["session_id"] == stale["session_id"] == str(session_id)
    assert fresh["provider_id"] == str(encounter.provider_id)
    assert fresh["exp"] > stale["exp"]


async def test_remint_returns_200_not_201(
    client: AsyncClient, fake_session: FakeSession, signing_key: str
) -> None:
    """201 would say a resource was created; nothing was."""
    session_id = uuid.uuid4()
    fake_session.existing = make_encounter(session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE)

    response = await client.post(
        f"/sessions/{session_id}/token", headers=credential(session_id=session_id, key=signing_key)
    )

    assert response.status_code == 200


async def test_remint_creates_no_encounter_row(
    client: AsyncClient, fake_session: FakeSession, signing_key: str
) -> None:
    """A second row is the fork this endpoint exists to prevent."""
    session_id = uuid.uuid4()
    fake_session.existing = make_encounter(session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE)

    await client.post(
        f"/sessions/{session_id}/token", headers=credential(session_id=session_id, key=signing_key)
    )

    assert fake_session.added == []


async def test_remint_publishes_nothing(
    client: AsyncClient, fake_session: FakeSession, fake_redis: FakeRedis, signing_key: str
) -> None:
    """A second sessions:started would make TASK-021 re-subscribe needlessly."""
    session_id = uuid.uuid4()
    fake_session.existing = make_encounter(session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE)

    await client.post(
        f"/sessions/{session_id}/token", headers=credential(session_id=session_id, key=signing_key)
    )

    assert fake_redis.published == []


async def test_remint_takes_the_provider_from_the_row_not_the_token(
    client: AsyncClient, fake_session: FakeSession, signing_key: str
) -> None:
    """A token claiming another provider must not change who the token is for."""
    session_id = uuid.uuid4()
    encounter = make_encounter(session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE)
    fake_session.existing = encounter
    impostor = uuid.uuid4()

    response = await client.post(
        f"/sessions/{session_id}/token",
        headers=credential(session_id=session_id, provider_id=impostor, key=signing_key),
    )

    claims = jwt.decode(response.json()["data"]["jwt"], signing_key, algorithms=[JWT_ALGORITHM])
    assert claims["provider_id"] == str(encounter.provider_id)
    assert claims["provider_id"] != str(impostor)


async def test_remint_audits_the_read_with_its_own_action(
    client: AsyncClient, fake_session: FakeSession, recorded_audit: RecordedAudit, signing_key: str
) -> None:
    """An audit has to tell 'visit opened' from 'token refreshed'."""
    session_id = uuid.uuid4()
    encounter = make_encounter(session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE)
    fake_session.existing = encounter

    await client.post(
        f"/sessions/{session_id}/token", headers=credential(session_id=session_id, key=signing_key)
    )

    assert recorded_audit.actions == [audit.ACTION_REMINT_SESSION_TOKEN]
    call = recorded_audit.calls[0]
    assert call["encounter_id"] == encounter.id
    assert call["provider_id"] == encounter.provider_id
    assert fake_session.commits == 1


async def test_remint_of_a_completed_session_is_409_and_yields_no_token(
    client: AsyncClient, fake_session: FakeSession, signing_key: str
) -> None:
    """A finished visit must not be able to reopen an audio socket."""
    session_id = uuid.uuid4()
    fake_session.existing = make_encounter(
        session_id=session_id,
        status=ENCOUNTER_STATUS_COMPLETED,
        ended_at=datetime.datetime(2026, 8, 18, 12, 30, tzinfo=datetime.UTC),
    )

    response = await client.post(
        f"/sessions/{session_id}/token", headers=credential(session_id=session_id, key=signing_key)
    )

    assert response.status_code == 409
    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == ERROR_CODE_SESSION_COMPLETED
    assert "jwt" not in response.text


async def test_remint_of_an_unknown_session_is_404(client: AsyncClient, signing_key: str) -> None:
    """fake_session.existing is None, standing in for no such row."""
    session_id = uuid.uuid4()

    response = await client.post(
        f"/sessions/{session_id}/token", headers=credential(session_id=session_id, key=signing_key)
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ERROR_CODE_SESSION_NOT_FOUND


class TestTheCredentialIsChecked:
    """401s, before the row is ever looked up."""

    async def test_a_request_with_no_authorization_header_is_401(
        self, client: AsyncClient, fake_session: FakeSession
    ) -> None:
        session_id = uuid.uuid4()
        fake_session.existing = make_encounter(
            session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE
        )

        response = await client.post(f"/sessions/{session_id}/token")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == ERROR_CODE_AUTH_REJECTED

    async def test_a_token_signed_with_another_key_is_401(
        self, client: AsyncClient, fake_session: FakeSession
    ) -> None:
        session_id = uuid.uuid4()
        fake_session.existing = make_encounter(
            session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE
        )

        response = await client.post(
            f"/sessions/{session_id}/token",
            headers=credential(session_id=session_id, key="an-entirely-different-32-byte-key"),
        )

        assert response.status_code == 401

    async def test_a_token_for_another_session_is_401(
        self, client: AsyncClient, fake_session: FakeSession, signing_key: str
    ) -> None:
        """Otherwise any live session could mint a token for any other."""
        session_id = uuid.uuid4()
        fake_session.existing = make_encounter(
            session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE
        )

        response = await client.post(
            f"/sessions/{session_id}/token",
            headers=credential(session_id=uuid.uuid4(), key=signing_key),
        )

        assert response.status_code == 401

    async def test_a_token_expired_beyond_the_grace_window_is_401(
        self, client: AsyncClient, fake_session: FakeSession, signing_key: str
    ) -> None:
        session_id = uuid.uuid4()
        fake_session.existing = make_encounter(
            session_id=session_id, status=ENCOUNTER_STATUS_ACTIVE
        )

        response = await client.post(
            f"/sessions/{session_id}/token",
            headers=credential(
                session_id=session_id,
                key=signing_key,
                expires_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=2),
            ),
        )

        assert response.status_code == 401

    async def test_the_rejection_never_echoes_the_token(
        self, client: AsyncClient, fake_session: FakeSession
    ) -> None:
        """A credential must not come back in an error body or reach a log."""
        session_id = uuid.uuid4()
        headers = credential(session_id=session_id, key="an-entirely-different-32-byte-key")

        response = await client.post(f"/sessions/{session_id}/token", headers=headers)

        assert headers["Authorization"].removeprefix("Bearer ") not in response.text

    async def test_a_bad_credential_is_refused_before_the_row_is_read(
        self, client: AsyncClient, fake_session: FakeSession
    ) -> None:
        """401 rather than 404 for an unknown session, so the endpoint is not a probe."""
        response = await client.post(f"/sessions/{uuid.uuid4()}/token")

        assert response.status_code == 401
        assert fake_session.commits == 0


async def test_remint_rejects_a_malformed_session_id(client: AsyncClient, signing_key: str) -> None:
    response = await client.post(
        "/sessions/not-a-uuid/token",
        headers=credential(session_id=uuid.uuid4(), key=signing_key),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("method", ["get", "put", "delete"])
async def test_remint_rejects_the_wrong_method(client: AsyncClient, method: str) -> None:
    response = await getattr(client, method)(f"/sessions/{uuid.uuid4()}/token")

    assert response.status_code == 405
