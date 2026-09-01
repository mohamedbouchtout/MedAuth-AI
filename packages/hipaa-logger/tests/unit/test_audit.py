"""Unit tests for audit_log().

asyncpg is mocked throughout — the pool and the connection, never a real database.
The integration test covers the real INSERT.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import hipaa_logger.db as db_module
from hipaa_logger import (
    AuditAction,
    AuditLogError,
    InvalidAuditFieldError,
    audit_log,
    set_connection,
)

ACTOR_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
SESSION_ID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
REQUEST_ID = "550e8400-e29b-41d4-a716-446655440000"
#: An EHR-asserted actor, as a SMART fhirUser claim gives it: an absolute
#: reference, and deliberately not a UUID — that is the whole reason this
#: column exists rather than the value going into actor_id.
PRACTITIONER_REF = "https://ehr.example.org/fhir/Practitioner/abc-123"


@pytest.fixture(autouse=True)
def _clear_injected_connection() -> Iterator[None]:
    """Keep injected state from leaking between tests."""
    set_connection(None)
    yield
    set_connection(None)


def make_connection() -> MagicMock:
    """A stand-in for asyncpg.Connection with an awaitable execute()."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


def make_pool(conn: MagicMock) -> MagicMock:
    """A stand-in for asyncpg.Pool whose acquire() yields `conn`."""

    @asynccontextmanager
    async def acquire() -> AsyncIterator[MagicMock]:
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    return pool


async def test_writes_through_the_pool_when_no_connection_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = make_connection()
    monkeypatch.setattr(db_module, "get_pool", AsyncMock(return_value=make_pool(conn)))

    await audit_log(
        actor_id=ACTOR_ID,
        action=AuditAction.READ_PATIENT,
        resource_type="Patient",
        resource_id="patient-123",
        session_id=SESSION_ID,
        service_name="track-b-rag",
    )

    conn.execute.assert_awaited_once()


async def test_supplied_connection_bypasses_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool_getter = AsyncMock()
    monkeypatch.setattr(db_module, "get_pool", pool_getter)
    conn = make_connection()

    await audit_log(
        actor_id=None,
        action=AuditAction.WRITE_NOTE,
        resource_type="ClinicalNote",
        resource_id="note-1",
        session_id=None,
        service_name="track-a-clinical",
        conn=conn,
    )

    conn.execute.assert_awaited_once()
    pool_getter.assert_not_awaited()


async def test_set_connection_is_used_when_no_conn_argument_is_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_getter = AsyncMock()
    monkeypatch.setattr(db_module, "get_pool", pool_getter)
    conn = make_connection()
    set_connection(conn)

    await audit_log(
        actor_id=None,
        action=AuditAction.READ_ENCOUNTER,
        resource_type="Coverage",
        resource_id="cov-1",
        session_id=None,
        service_name="fhir-integration",
    )

    conn.execute.assert_awaited_once()
    pool_getter.assert_not_awaited()


async def test_parameters_are_bound_in_column_order() -> None:
    conn = make_connection()

    await audit_log(
        actor_id=ACTOR_ID,
        action=AuditAction.SUBMIT_PRIOR_AUTH,
        resource_type="Claim",
        resource_id="claim-9",
        session_id=SESSION_ID,
        service_name="prior-auth",
        request_id=REQUEST_ID,
        ip_address="198.51.100.7",
        user_agent="MedAuth/1.0",
        fhir_practitioner_ref=PRACTITIONER_REF,
        conn=conn,
    )

    _, *params = conn.execute.await_args.args
    assert params == [
        uuid.UUID(ACTOR_ID),
        "SUBMIT_PRIOR_AUTH",
        "Claim",
        "claim-9",
        uuid.UUID(SESSION_ID),
        "prior-auth",
        uuid.UUID(REQUEST_ID),
        ipaddress.ip_address("198.51.100.7"),
        "MedAuth/1.0",
        PRACTITIONER_REF,
    ]


async def test_optional_fields_default_to_none() -> None:
    conn = make_connection()

    await audit_log(
        actor_id=None,
        action=AuditAction.READ_PATIENT,
        resource_type=None,
        resource_id=None,
        session_id=None,
        service_name="audio-ingestion",
        conn=conn,
    )

    _, *params = conn.execute.await_args.args
    assert params[6:] == [None, None, None, None]


async def test_ipv6_address_is_accepted() -> None:
    conn = make_connection()

    await audit_log(
        actor_id=None,
        action=AuditAction.READ_PATIENT,
        resource_type="Patient",
        resource_id="p-1",
        session_id=None,
        service_name="nudge-service",
        ip_address="2001:db8::1",
        conn=conn,
    )

    _, *params = conn.execute.await_args.args
    assert params[7] == ipaddress.ip_address("2001:db8::1")


async def test_database_failure_is_raised_never_swallowed() -> None:
    conn = make_connection()
    conn.execute = AsyncMock(side_effect=OSError("connection reset"))

    with pytest.raises(AuditLogError, match="Failed to write audit event"):
        await audit_log(
            actor_id=None,
            action=AuditAction.READ_PATIENT,
            resource_type="Patient",
            resource_id="p-1",
            session_id=None,
            service_name="track-b-rag",
            conn=conn,
        )


async def test_original_exception_is_preserved_as_the_cause() -> None:
    conn = make_connection()
    original = OSError("connection reset")
    conn.execute = AsyncMock(side_effect=original)

    with pytest.raises(AuditLogError) as excinfo:
        await audit_log(
            actor_id=None,
            action=AuditAction.READ_PATIENT,
            resource_type="Patient",
            resource_id="p-1",
            session_id=None,
            service_name="track-b-rag",
            conn=conn,
        )

    assert excinfo.value.__cause__ is original


@pytest.mark.parametrize("field", ["actor_id", "session_id", "request_id"])
async def test_malformed_uuid_raises_before_any_write(field: str) -> None:
    conn = make_connection()
    kwargs: dict[str, Any] = {
        "actor_id": None,
        "action": AuditAction.READ_PATIENT,
        "resource_type": "Patient",
        "resource_id": "p-1",
        "session_id": None,
        "service_name": "track-b-rag",
        "conn": conn,
    }
    kwargs[field] = "not-a-uuid"

    with pytest.raises(InvalidAuditFieldError, match=field):
        await audit_log(**kwargs)

    conn.execute.assert_not_awaited()


async def test_malformed_ip_raises_before_any_write() -> None:
    conn = make_connection()

    with pytest.raises(InvalidAuditFieldError, match="ip_address"):
        await audit_log(
            actor_id=None,
            action=AuditAction.READ_PATIENT,
            resource_type="Patient",
            resource_id="p-1",
            session_id=None,
            service_name="track-b-rag",
            ip_address="999.999.999.999",
            conn=conn,
        )

    conn.execute.assert_not_awaited()


async def test_service_name_is_required() -> None:
    """Split out from a parametrized pair that also covered an empty action.

    An empty action is now refused by the vocabulary check, which raises before
    service_name is ever looked at — so the old case would have passed for a
    reason that had nothing to do with what it was named for.
    """
    conn = make_connection()

    with pytest.raises(InvalidAuditFieldError, match="service_name"):
        await audit_log(
            actor_id=None,
            action=AuditAction.READ_PATIENT,
            resource_type="Patient",
            resource_id="p-1",
            session_id=None,
            service_name="",
            conn=conn,
        )

    conn.execute.assert_not_awaited()


@pytest.mark.parametrize("action", ["", "READ", "POLICY_QUERY", "read_patient"])
async def test_an_action_outside_the_vocabulary_is_refused(action: str) -> None:
    """The runtime half of the single-source rule.

    mypy rejects these at the call site; this is the backstop for callers static
    typing does not reach. ``POLICY_QUERY`` is the case that matters: a
    transposition of a real member, which the free-text column would have
    accepted happily and which would then have to be known about forever by
    anyone querying the trail.
    """
    conn = make_connection()

    with pytest.raises(InvalidAuditFieldError, match="vocabulary"):
        await audit_log(
            actor_id=None,
            action=action,  # type: ignore[arg-type]  # the point of the test
            resource_type="Patient",
            resource_id="p-1",
            session_id=None,
            service_name="track-b-rag",
            conn=conn,
        )

    conn.execute.assert_not_awaited()


async def test_a_vocabulary_member_reaches_the_database_as_plain_text() -> None:
    """Nothing downstream should have to know the vocabulary is an enum.

    The column is text and reads back as text — an audit query, a report, or a
    future consumer compares against ``"READ_PATIENT"``, not against a Python
    type. A string spelling a real member is accepted for the same reason.
    """
    conn = make_connection()

    await audit_log(
        actor_id=None,
        action="READ_PATIENT",  # type: ignore[arg-type]  # accepted, then coerced
        resource_type="Patient",
        resource_id="p-1",
        session_id=None,
        service_name="track-b-rag",
        conn=conn,
    )

    written = conn.execute.await_args.args[2]
    assert written == "READ_PATIENT"
    assert type(written) is str


async def test_uuid_objects_are_accepted_as_well_as_strings() -> None:
    conn = make_connection()
    actor = uuid.UUID(ACTOR_ID)

    await audit_log(
        actor_id=actor,  # type: ignore[arg-type]  # documented convenience
        action=AuditAction.READ_PATIENT,
        resource_type="Patient",
        resource_id="p-1",
        session_id=None,
        service_name="track-b-rag",
        conn=conn,
    )

    _, *params = conn.execute.await_args.args
    assert params[0] == actor


class TestTheEhrAssertedActor:
    """The fhir_practitioner_ref column — CLAUDE.md, "The EHR-asserted actor is
    its own column"."""

    async def test_a_non_uuid_reference_is_accepted(self) -> None:
        """The reason the column exists at all.

        A FHIR Practitioner id is [A-Za-z0-9\\-\\.]{1,64} — HAPI issues "1",
        Epic issues opaque strings. Passing one as actor_id raises; this column
        takes it as it comes.
        """
        conn = make_connection()

        await audit_log(
            actor_id=None,
            action=AuditAction.READ_PATIENT,
            resource_type="Patient",
            resource_id="p-1",
            session_id=None,
            service_name="fhir-integration",
            fhir_practitioner_ref="Practitioner/1",
            conn=conn,
        )

        _, *params = conn.execute.await_args.args
        assert params[9] == "Practitioner/1"

    async def test_the_same_value_is_refused_as_actor_id(self) -> None:
        """The two columns are not interchangeable, and the type says so.

        This is the failure the separate column exists to prevent: it would have
        fired on exactly the launches where capturing the claim succeeded.
        """
        conn = make_connection()

        with pytest.raises(InvalidAuditFieldError, match="actor_id"):
            await audit_log(
                actor_id="Practitioner/1",
                action=AuditAction.READ_PATIENT,
                resource_type="Patient",
                resource_id="p-1",
                session_id=None,
                service_name="fhir-integration",
                conn=conn,
            )

        conn.execute.assert_not_awaited()

    async def test_an_absolute_reference_is_stored_verbatim(self) -> None:
        """Stored whole, not reduced to the bare id.

        A Practitioner id is unique only within one EHR, so dropping the server
        would silently merge two providers into one audit identity.
        """
        conn = make_connection()

        await audit_log(
            actor_id=None,
            action=AuditAction.READ_ENCOUNTER,
            resource_type="Encounter",
            resource_id="e-1",
            session_id=None,
            service_name="fhir-integration",
            fhir_practitioner_ref=PRACTITIONER_REF,
            conn=conn,
        )

        _, *params = conn.execute.await_args.args
        assert params[9] == PRACTITIONER_REF

    async def test_surrounding_whitespace_is_stripped(self) -> None:
        conn = make_connection()

        await audit_log(
            actor_id=None,
            action=AuditAction.READ_PATIENT,
            resource_type="Patient",
            resource_id="p-1",
            session_id=None,
            service_name="fhir-integration",
            fhir_practitioner_ref=f"  {PRACTITIONER_REF}\n",
            conn=conn,
        )

        _, *params = conn.execute.await_args.args
        assert params[9] == PRACTITIONER_REF

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    async def test_an_empty_reference_is_refused_rather_than_written(self, value: str) -> None:
        """Pass None for an unknown actor — an empty string reads as an identity."""
        conn = make_connection()

        with pytest.raises(InvalidAuditFieldError, match="fhir_practitioner_ref"):
            await audit_log(
                actor_id=None,
                action=AuditAction.READ_PATIENT,
                resource_type="Patient",
                resource_id="p-1",
                session_id=None,
                service_name="fhir-integration",
                fhir_practitioner_ref=value,
                conn=conn,
            )

        conn.execute.assert_not_awaited()

    async def test_a_reference_too_long_for_the_column_is_refused(self) -> None:
        """Named here rather than surfacing as a database error naming a column."""
        conn = make_connection()

        with pytest.raises(InvalidAuditFieldError, match="512"):
            await audit_log(
                actor_id=None,
                action=AuditAction.READ_PATIENT,
                resource_type="Patient",
                resource_id="p-1",
                session_id=None,
                service_name="fhir-integration",
                fhir_practitioner_ref="https://ehr.example.org/fhir/Practitioner/" + ("x" * 512),
                conn=conn,
            )

        conn.execute.assert_not_awaited()

    async def test_it_defaults_to_none_so_existing_callers_are_unaffected(self) -> None:
        """Every audit call in the repository predates this column.

        None of them passes it, and each must keep writing a row with a null
        there rather than needing to be touched.
        """
        conn = make_connection()

        await audit_log(
            actor_id=ACTOR_ID,
            action=AuditAction.WRITE_NOTE,
            resource_type="ClinicalNote",
            resource_id="note-1",
            session_id=SESSION_ID,
            service_name="track-a-clinical",
            conn=conn,
        )

        _, *params = conn.execute.await_args.args
        assert params[9] is None

    async def test_it_is_never_populated_from_actor_id(self) -> None:
        """Neither column is a fallback for the other.

        A row with a UUID actor and no EHR assertion leaves this null; the
        reverse leaves actor_id null. An audit query has to read both.
        """
        conn = make_connection()

        await audit_log(
            actor_id=ACTOR_ID,
            action=AuditAction.READ_PATIENT,
            resource_type="Patient",
            resource_id="p-1",
            session_id=None,
            service_name="track-a-clinical",
            conn=conn,
        )

        _, *params = conn.execute.await_args.args
        assert params[0] == uuid.UUID(ACTOR_ID)
        assert params[9] is None

    async def test_a_non_string_reference_is_refused(self) -> None:
        """Defensive, like the UUID and IP coercions beside it.

        mypy rejects this at a typed call site; the check is here for the
        callers static typing does not reach.
        """
        conn = make_connection()

        with pytest.raises(InvalidAuditFieldError, match="fhir_practitioner_ref"):
            await audit_log(
                actor_id=None,
                action=AuditAction.READ_PATIENT,
                resource_type="Patient",
                resource_id="p-1",
                session_id=None,
                service_name="fhir-integration",
                fhir_practitioner_ref=uuid.UUID(ACTOR_ID),  # type: ignore[arg-type]
                conn=conn,
            )

        conn.execute.assert_not_awaited()
