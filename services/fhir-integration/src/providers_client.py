"""Resolving an EHR practitioner to a ``provider_id`` in ``track-a-clinical``.

TASK-025b. A SMART launch tells us who authorized it — TASK-051c verifies the
``fhirUser`` claim against the EHR's published keys and stores the resulting
``Practitioner`` reference on the launch record. ``POST /sessions/start`` needs a
``provider_id`` UUID, and a FHIR ``Practitioner`` id is not one: HAPI answers
``"1"``. The registry that maps between them belongs to ``track-a-clinical``,
which owns the core schema's migration history, so this service asks rather than
writing the row itself.

**It goes over HTTP even though both services share a virtualenv**, exactly as
``notes_client`` does and for a related reason: this service holds no database
connection of its own and deliberately never has. Importing that service's
storage functions to save a hop would give it one.

**A practitioner reference is not PHI**, so nothing here audits and the call
writes no row on either side. It identifies the provider, not a patient. It is
still an identifier naming an individual clinician, so it is kept out of log
lines for the same reason a `launch_id` is.

**A failure here is not a failure of the launch.** Everything raises
:class:`ProviderServiceError`, and the caller decides — for
``GET /fhir/launch-context`` that means answering with a null ``provider_id``
rather than failing a launch that is working perfectly.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

#: Where the registry answers. A constant rather than a literal at the call site
#: so a change to the path is one edit, as in ``notes_client``.
RESOLVE_PATH: Final = "/providers/resolve"


class ProviderServiceError(Exception):
    """``track-a-clinical`` could not be reached, or did not answer usably.

    Attributes:
        detail: A fixed description of what failed. Never derived from a response
            body, and never carrying the practitioner reference.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ResolvedProvider(BaseModel):
    """The registry's answer.

    Attributes:
        provider_id: The UUID ``POST /sessions/start`` takes.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    provider_id: str


class ProvidersClient:
    """The one call this service makes to the provider registry."""

    def __init__(self, base_url: str, http_client: httpx.AsyncClient, timeout: float) -> None:
        """Bind the client to the registry's address.

        Args:
            base_url: Where ``track-a-clinical`` answers.
            http_client: The shared, pooled HTTP client.
            timeout: Per-call timeout, set here rather than on the shared client.
        """
        self._base_url = base_url.rstrip("/")
        self._http = http_client
        self._timeout = timeout

    async def resolve(self, fhir_practitioner_ref: str) -> str:
        """Return the ``provider_id`` for one verified practitioner reference.

        Args:
            fhir_practitioner_ref: The reference as the verified ``fhirUser``
                claim gave it — an absolute URL, not a bare id.

        Returns:
            The provider's UUID, as a string.

        Raises:
            ProviderServiceError: The registry could not be reached, refused the
                request, or answered with something unusable.
        """
        if not self._base_url:
            raise ProviderServiceError("track-a-clinical is not configured")

        try:
            response = await self._http.post(
                f"{self._base_url}{RESOLVE_PATH}",
                json={"fhir_practitioner_ref": fhir_practitioner_ref},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderServiceError("track-a-clinical did not answer in time") from exc
        except httpx.HTTPError as exc:
            # Deliberately not ``str(exc)``: httpx puts the request URL in its
            # message. That one carries no identifier today, and the rule holds
            # regardless — the reference is never allowed near a log line.
            raise ProviderServiceError("track-a-clinical could not be reached") from exc

        if response.status_code >= 400:
            logger.warning(
                "track-a-clinical answered %s for a provider resolution.", response.status_code
            )
            raise ProviderServiceError("track-a-clinical refused the request")

        try:
            body: Any = response.json()
        except ValueError as exc:
            raise ProviderServiceError("track-a-clinical's response was not JSON") from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise ProviderServiceError("track-a-clinical's response carried no data")

        try:
            return ResolvedProvider.model_validate(data).provider_id
        except ValidationError as exc:
            raise ProviderServiceError(
                "track-a-clinical's provider payload was not usable"
            ) from exc
