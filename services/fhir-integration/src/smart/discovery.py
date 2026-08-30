"""Finding an EHR's authorization and token endpoints.

Discovery is ``{iss}/.well-known/smart-configuration`` and nothing else. The
older pattern — reading ``oauth-uris`` extensions off the server's
CapabilityStatement — is explicitly out of scope for v1 per TASK-051, so a
server offering only that fails here with a message naming the document it did
not serve and the issuer host it was asked for. The point of naming both is that
a vendor sandbox which cannot be launched against says why in one line, instead
of surfacing as a ``KeyError`` or a bare 502 that someone has to reproduce to
understand.
"""

from __future__ import annotations

import logging
from typing import Final

import httpx
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

#: The SMART discovery document, relative to the FHIR base URL.
SMART_CONFIGURATION_PATH: Final = ".well-known/smart-configuration"

#: How long to wait on an EHR's discovery document. A launch is a person waiting
#: on a browser redirect, so a hung request is worse than a named failure; ten
#: seconds is a round default rather than a measured one.
DISCOVERY_TIMEOUT_SECONDS: Final = 10.0


class SmartConfiguration(BaseModel):
    """The two endpoints a launch needs, from an EHR's discovery document.

    Deliberately narrow. The document carries capabilities, supported scopes,
    grant types and more, and none of it is read here — a launch needs somewhere
    to send the browser and somewhere to exchange the code. Parsing fields
    nothing uses would mean a vendor that omits one breaks a launch that never
    needed it.
    """

    authorization_endpoint: str = Field(min_length=1)
    token_endpoint: str = Field(min_length=1)


class DiscoveryError(RuntimeError):
    """An EHR's SMART configuration could not be read.

    Carries the issuer host rather than the full ``iss``: a launch URL can hold
    context in its query string, and this message reaches a log line.
    """

    def __init__(self, issuer_host: str, detail: str) -> None:
        self.issuer_host = issuer_host
        self.detail = detail
        super().__init__(
            f"No usable SMART configuration at {issuer_host}/{SMART_CONFIGURATION_PATH}: "
            f"{detail}. This service reads that document only — the older "
            "CapabilityStatement oauth-uris extension is not supported (TASK-051)."
        )


def discovery_url(fhir_base_url: str) -> str:
    """Return the discovery document's URL for one FHIR base URL."""
    return f"{fhir_base_url.rstrip('/')}/{SMART_CONFIGURATION_PATH}"


async def fetch_smart_configuration(
    client: httpx.AsyncClient,
    fhir_base_url: str,
    *,
    issuer_host: str,
) -> SmartConfiguration:
    """Read an EHR's authorization and token endpoints.

    Args:
        client: The HTTP client to fetch with.
        fhir_base_url: The FHIR base URL, which for a SMART launch is ``iss``.
        issuer_host: The host, for messages and logs. Never the full URL.

    Returns:
        The two endpoints the launch needs.

    Raises:
        DiscoveryError: If the document is unreachable, is not JSON, or does not
            carry both endpoints. Every failure names the document and the host.
    """
    url = discovery_url(fhir_base_url)
    try:
        response = await client.get(
            url,
            headers={"Accept": "application/json"},
            timeout=DISCOVERY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        # str(exc) can contain the request URL, which is built from iss. The
        # host is already in the message and the class name says what broke;
        # neither adds the query string.
        raise DiscoveryError(issuer_host, f"request failed ({type(exc).__name__})") from exc

    if response.status_code != httpx.codes.OK:
        raise DiscoveryError(issuer_host, f"server answered HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise DiscoveryError(issuer_host, "response was not JSON") from exc

    try:
        return SmartConfiguration.model_validate(payload)
    except ValidationError as exc:
        missing = ", ".join(str(error["loc"][0]) for error in exc.errors() if error["loc"])
        fields = missing or "authorization_endpoint, token_endpoint"
        raise DiscoveryError(issuer_host, f"document is missing or empty at: {fields}") from exc
