"""Athenahealth. First EHR to certify against — see CLAUDE.md's priority order."""

from __future__ import annotations

import httpx

from .base import EHRAdapter
from .covermymeds import CoverMyMedsClient, CoverMyMedsNotConfigured
from .models import PriorAuthContent, PriorAuthSubmission


class AthenaAdapter(EHRAdapter):
    """Athenahealth's adapter.

    One override: :meth:`submit_prior_auth`. Athenahealth does not support FHIR
    PAS, so prior authorizations go through the CoverMyMeds API instead
    (TASK-054). Everything else is standard FHIR and stays on the base class,
    which is the rule this layer exists for — if code only works on one EHR it
    belongs in a subclass, and if it works on all of them it belongs in the base.
    TASK-055 records any further Athenahealth quirks here.

    **The vendor client is constructed by the factory and injected**, not built
    here. The factory is the only place that knows which vendor needs which extra
    configuration, which is exactly its job; the alternative was a CoverMyMeds
    parameter on the base adapter's constructor, which would put one vendor's
    concern in the class every other vendor inherits.
    """

    def __init__(
        self,
        fhir_base_url: str,
        access_token: str,
        http_client: httpx.AsyncClient,
        covermymeds: CoverMyMedsClient | None = None,
    ) -> None:
        """Bind the adapter, and the CoverMyMeds path its submissions take.

        Args:
            fhir_base_url: Base URL of Athenahealth's FHIR R4 server.
            access_token: The SMART on FHIR access token for this launch.
            http_client: The shared HTTP client. See :class:`EHRAdapter`.
            covermymeds: The prior-authorization path. ``None`` when nothing
                configured one, which :meth:`submit_prior_auth` reports plainly
                rather than discovering inside an HTTP call.
        """
        super().__init__(fhir_base_url, access_token, http_client)
        self._covermymeds = covermymeds

    async def submit_prior_auth(self, content: PriorAuthContent) -> PriorAuthSubmission:
        """Submit through CoverMyMeds rather than FHIR ``Claim/$submit``.

        Athenahealth's FHIR server does not implement the PAS operation, so the
        base class's path would post to an endpoint that answers 404. This is the
        whole reason the submission is a method on the adapter rather than a
        function the route calls: the route asks the adapter to submit and gets
        the right path without knowing which EHR it is talking to.

        **The field-level translation this delegates to is explicitly
        unverified** — see ``covermymeds.py``, which says exactly what has never
        been checked against the real API. What is settled here is the seam.

        Args:
            content: The request in this system's own terms. Codes arrive
                unfiltered; the vendor client filters them, on the same terms as
                the FHIR builder.

        Returns:
            What the vendor said, its reference when it gave one, and
            ``covermymeds`` as the submission method.

        Raises:
            CoverMyMedsNotConfigured: The path is not configured at all.
            FHIRAuthorizationExpired: The vendor rejected the credential.
            FHIRUpstreamUnavailable: Transport failure, timeout or a 5xx.
            FHIRMalformedResponse: The vendor refused the request or answered
                with a status this integration does not recognise.
        """
        if self._covermymeds is None:
            raise CoverMyMedsNotConfigured(
                "COVERMYMEDS_BASE_URL and COVERMYMEDS_API_KEY are not both set"
            )
        return await self._covermymeds.submit(content)
