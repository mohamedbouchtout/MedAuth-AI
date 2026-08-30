"""Runtime configuration for fhir-integration.

Values come from the process environment only, for the reasons given in
``track_a_clinical.config``: local development exports them from ``.env.local``,
CI sets them on the job, deployments inject them from AWS Secrets Manager, and
reading a file here would add a fourth source of truth and a tempting place to
commit a secret.

**Client secrets are ``SecretStr``.** TASK-050 requires an access token to stay
out of every log line, exception message and ``repr``; a client secret is the
credential that obtains those tokens, so it gets the same treatment by
construction rather than by everyone remembering. ``SecretStr`` renders as
``**********`` wherever a settings object is formatted, which is what makes a
stray ``logger.debug("%s", settings)`` harmless instead of a disclosure.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.adapters.factory import EHRType


class ClientCredentials(BaseModel):
    """One EHR's registered SMART client registration.

    ``client_secret`` is optional because SMART on FHIR 2.0 recognises public
    clients, which authenticate at the token endpoint with PKCE alone. A vendor
    whose secret is unset is treated as a public client rather than as a
    misconfiguration — that is a real deployment shape, not an oversight.
    ``client_id`` being unset is the misconfiguration, and is what
    ``credentials_for()`` refuses on.
    """

    client_id: str
    client_secret: SecretStr | None = None


#: Which settings prefix holds each vendor's registration. Keyed on ``EHRType``
#: so the closed vocabulary TASK-050 established stays the only vendor
#: identifier in the flow — a second lookup keyed on a raw string would be a
#: fourth spelling of an identity this repository has already made an enum three
#: times. ``tests/unit/test_config.py`` asserts every member appears here, so a
#: key cannot be added to ``EHRType`` without credentials behind it.
_CREDENTIAL_PREFIXES: dict[EHRType, str] = {
    EHRType.ATHENA: "athena",
    EHRType.ECW: "ecw",
    EHRType.MODMED: "modmed",
    EHRType.CERNER: "cerner",
    EHRType.EPIC: "epic",
    EHRType.GENERIC: "generic",
}


class Settings(BaseSettings):
    """Environment-backed settings for the SMART on FHIR launch flow."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    redis_url: str = Field(default="redis://localhost:6379/0", min_length=1)

    #: Where the EHR's authorization server sends the browser back to. This must
    #: be byte-for-byte what was registered in each vendor's developer portal:
    #: a mismatch is rejected by the authorization server before any request
    #: reaches this service, so there is nothing in our logs to diagnose it from.
    #: See TASK-051, which makes verifying it against the registered value an
    #: explicit step rather than something discovered during a failed launch.
    smart_redirect_uri: str = Field(default="", min_length=0)

    #: Space-separated scopes requested on every launch, minus the launch-shaped
    #: one — ``launch`` and ``launch/patient`` are appended per launch type by
    #: ``authorization_scopes()`` below, because which of the two applies is a
    #: property of the launch rather than of the deployment.
    smart_scopes: str = Field(default="openid fhirUser offline_access user/*.read")

    #: How long a launch may sit between the authorization redirect and the
    #: callback that consumes it. A round default matching the "~10 min" in
    #: CLAUDE.md's Redis key list, not a measurement: it is an upper bound on a
    #: human completing a login, and no real flow has been timed against it.
    smart_launch_ttl_seconds: int = Field(default=600, gt=0)

    athena_client_id: str = ""
    athena_client_secret: SecretStr | None = None
    ecw_client_id: str = ""
    ecw_client_secret: SecretStr | None = None
    modmed_client_id: str = ""
    modmed_client_secret: SecretStr | None = None
    cerner_client_id: str = ""
    cerner_client_secret: SecretStr | None = None
    epic_client_id: str = ""
    epic_client_secret: SecretStr | None = None

    #: The registration used for an issuer ``detect_ehr_from_issuer()`` did not
    #: recognise, and for the local HAPI FHIR server in development. TASK-050
    #: routes an unknown issuer to the standard FHIR adapter rather than
    #: failing, and that reasoning holds once a token exists — but obtaining one
    #: is a registration, and a client we never registered has no credentials to
    #: present. So an unrecognised issuer launches when this pair is configured
    #: and is refused with a named error when it is not, which is the honest
    #: answer rather than a launch that fails somewhere inside the vendor's
    #: authorization server.
    generic_client_id: str = ""
    generic_client_secret: SecretStr | None = None

    def credentials_for(self, ehr_type: EHRType) -> ClientCredentials:
        """Return the registered client for one EHR.

        Args:
            ehr_type: The vendor key from ``detect_ehr_from_issuer()``.

        Returns:
            That vendor's ``client_id`` and, for a confidential client, its
            secret.

        Raises:
            MissingClientCredentialsError: If no ``client_id`` is configured for
                this vendor. The message names the environment variable to set;
                it never names or hints at a secret.
        """
        prefix = _CREDENTIAL_PREFIXES[ehr_type]
        client_id: str = getattr(self, f"{prefix}_client_id")
        if not client_id:
            raise MissingClientCredentialsError(ehr_type, f"{prefix.upper()}_CLIENT_ID")

        secret: SecretStr | None = getattr(self, f"{prefix}_client_secret")
        return ClientCredentials(client_id=client_id, client_secret=secret)

    def authorization_scopes(self, *, ehr_launch: bool) -> str:
        """Return the scope string for one launch.

        The two SMART launch types differ here and only here: an EHR launch
        asks for ``launch``, which redeems the opaque ``launch`` parameter the
        EHR supplied for its context, while a standalone launch has no such
        parameter and asks for ``launch/patient`` so the authorization server
        prompts for a patient instead.

        Args:
            ehr_launch: Whether the EHR supplied a ``launch`` parameter.

        Returns:
            The space-separated scope string to send to the authorization
            endpoint.
        """
        launch_scope = "launch" if ehr_launch else "launch/patient"
        return " ".join((*self.smart_scopes.split(), launch_scope))


class MissingClientCredentialsError(RuntimeError):
    """No SMART client is registered for the EHR a launch resolved to."""

    def __init__(self, ehr_type: EHRType, variable: str) -> None:
        self.ehr_type = ehr_type
        self.variable = variable
        super().__init__(
            f"No SMART client registered for EHR {ehr_type.value!r}: set {variable}. "
            "An unrecognised issuer uses the GENERIC_* pair."
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once.

    Tests that change the environment must call ``get_settings.cache_clear()``.
    """
    return Settings()
