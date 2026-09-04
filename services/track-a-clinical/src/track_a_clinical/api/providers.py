r"""The provider registry — turning an EHR practitioner into a ``provider_id`` (TASK-025b).

``encounters.provider_id`` is a UUID. The identity a SMART launch gives us is a
FHIR ``Practitioner`` reference, whose id is ``[A-Za-z0-9\-\.]{1,64}`` and is
routinely not a UUID — HAPI answers ``"1"``. TASK-051c verifies that identity
against the EHR's published keys and stores the reference on the launch record,
and until this route existed nothing could turn it into a value the core schema
keys on. So a visit could be launched by a known provider and still not be
startable.

**Server-to-server, and deliberately not reachable from a browser.**
``fhir-integration`` calls this while answering ``GET /fhir/launch-context``, and
that route hands an app the resolved ``provider_id`` — never the reference. A
client that could present a practitioner reference here would be asserting a
provider identity of its own, which is the thing "the provider comes from the
``encounters`` row, never from the presented token's claim" exists to prevent, one
step earlier. It is absent from ``CORS_ALLOWED_ORIGINS`` handling for the same
reason: no browser has business calling it.

**It writes no audit row, and that is the rule rather than an omission.** A
practitioner reference identifies the *provider*, not a patient, so nothing here
touches PHI. Known Constraints #6 is an if-and-only-if in both directions — an
operational write mixed into ``audit_log`` turns "who accessed patient X" from a
query you can run into one you have to filter. It logs at INFO instead, like
``POST /policies/ingest``.

**Resolution is get-or-create and is idempotent.** One practitioner is one
provider forever: a clinician launching MedAuth twice on Tuesday must key both
visits to the same row, or their encounters split in two with nothing erroring.
The uniqueness is enforced by the database rather than by this module, because
two launches can race.
"""

from __future__ import annotations

import logging
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api_envelope import ApiResponse, error_responses
from track_a_clinical.api.dependencies import get_db_session
from track_a_clinical.api.schemas import ProviderData, ResolveProviderRequest
from track_a_clinical.models import Provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/providers", tags=["providers"])


@router.post(
    "/resolve",
    response_model=ApiResponse[ProviderData],
    status_code=status.HTTP_200_OK,
    summary="Resolve an EHR practitioner reference to a provider id",
    response_description="The provider id for this practitioner.",
    responses=error_responses(
        422,
        descriptions={422: "The practitioner reference was missing or too long."},
    ),
)
async def resolve_provider(
    body: ResolveProviderRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse[ProviderData]:
    """Return the ``provider_id`` for one practitioner, creating the row if new.

    **200 rather than 201, whichever happened.** A caller resolving an identity
    wants the identifier, and cannot use "was this the first time this clinician
    launched MedAuth" for anything. Reporting 201 on the first call would also
    make the status depend on unrelated history, so a client that branched on it
    would behave differently for a new hire than for everyone else.

    **The insert races, and the race is handled in the database.** Two launches
    by one practitioner can arrive together; ``ON CONFLICT DO NOTHING`` lets the
    loser insert nothing and read the winner's row, so both answer the same id.
    A check-then-insert in Python would produce two rows and split one
    clinician's encounters between them.
    """
    reference = body.fhir_practitioner_ref

    # The insert first, so the common case is one round trip. DO NOTHING rather
    # than DO UPDATE: there is no field to update, and an update would churn the
    # row on every launch for no benefit.
    inserted = await session.execute(
        pg_insert(Provider)
        .values(fhir_practitioner_ref=reference)
        .on_conflict_do_nothing(index_elements=["fhir_practitioner_ref"])
        .returning(Provider.id)
    )
    provider_id = inserted.scalar_one_or_none()

    if provider_id is None:
        # Either another writer won the race or the row already existed. Both
        # mean the row is there to be read. A soft-deleted provider is read back
        # unchanged rather than resurrected or duplicated: the unique constraint
        # covers the column regardless of `deleted_at`, so a second row is not
        # available even if it were wanted, and reviving one is a decision for
        # whoever retired it.
        existing = await session.execute(
            sa.select(Provider.id).where(Provider.fhir_practitioner_ref == reference)
        )
        provider_id = existing.scalar_one()
    else:
        # Not the reference itself: it names an individual clinician, and an
        # operational log is not the place to accumulate a roster of who used the
        # product and when.
        logger.info("Registered a provider from a newly seen EHR practitioner reference.")

    await session.commit()
    return ApiResponse[ProviderData](data=ProviderData(provider_id=provider_id))
