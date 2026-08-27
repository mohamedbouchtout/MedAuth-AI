"""Request and response bodies for the session lifecycle endpoints."""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field


class StartSessionRequest(BaseModel):
    """Body of ``POST /sessions/start``.

    ``patient_id`` is the wire name for what the schema stores as
    ``encounters.patient_fhir_id`` — the names differ deliberately, see CLAUDE.md
    "Session Lifecycle & JWT Issuance". No ``session_id`` field exists: the
    server generates it, and accepting a client-supplied one would let a caller
    collide with or impersonate another encounter's session.
    """

    model_config = ConfigDict(extra="forbid")

    patient_id: str = Field(min_length=1, max_length=100)
    provider_id: uuid.UUID
    ehr_encounter_id: str | None = Field(default=None, max_length=100)


class StartSessionData(BaseModel):
    """``data`` payload returned by ``POST /sessions/start``."""

    session_id: uuid.UUID
    jwt: str


class EndSessionData(BaseModel):
    """``data`` payload returned by ``POST /sessions/{session_id}/end``.

    ``already_ended`` is true when the call was a no-op repeat. Callers do not
    need it to behave correctly — the endpoint is idempotent either way — but it
    makes a duplicate client retry visible instead of silent.
    """

    session_id: uuid.UUID
    status: str
    ended_at: datetime.datetime
    already_ended: bool


class RemintTokenData(BaseModel):
    """``data`` payload returned by ``POST /sessions/{session_id}/token``.

    Structurally identical to :class:`StartSessionData` and deliberately a
    separate model: the published spec should not describe a re-mint response as
    "StartSessionData", when the entire point of the endpoint is that it starts
    nothing. ``session_id`` is echoed back so a client can assert it got a token
    for the session it asked about rather than a new one.
    """

    session_id: uuid.UUID
    jwt: str
