"""Settings, and the rule that every EHR in the vocabulary has credentials behind it."""

from __future__ import annotations

import pytest

from src.adapters.factory import EHRType
from src.config import (
    MissingClientCredentialsError,
    Settings,
    get_settings,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


class TestCredentialSelection:
    """The flow keys off EHRType and introduces no second vendor identifier."""

    def test_every_ehr_type_member_has_a_credential_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key added to EHRType with no credentials behind it would be a KeyError
        at a real SMART launch — the same exhaustiveness TASK-050 asserts for the
        adapter table, one layer along.
        """
        for member in EHRType:
            monkeypatch.setenv(f"{member.value.upper()}_CLIENT_ID", f"{member.value}-id")

        settings = Settings()

        for member in EHRType:
            assert settings.credentials_for(member).client_id == f"{member.value}-id"

    def test_an_unconfigured_vendor_names_the_variable_to_set(self) -> None:
        settings = Settings()

        with pytest.raises(MissingClientCredentialsError) as caught:
            settings.credentials_for(EHRType.CERNER)

        assert caught.value.variable == "CERNER_CLIENT_ID"
        assert "CERNER_CLIENT_ID" in str(caught.value)

    def test_a_registration_without_a_secret_is_a_public_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SMART on FHIR 2.0 recognises public clients; PKCE is what covers them."""
        monkeypatch.setenv("EPIC_CLIENT_ID", "epic-id")

        credentials = Settings().credentials_for(EHRType.EPIC)

        assert credentials.client_secret is None


class TestSecretsDoNotRender:
    """A client secret obtains access tokens, so it gets an access token's treatment."""

    def test_a_secret_does_not_appear_in_the_settings_repr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENA_CLIENT_ID", "athena-id")
        monkeypatch.setenv("ATHENA_CLIENT_SECRET", "super-secret-value")

        settings = Settings()

        assert "super-secret-value" not in repr(settings)
        assert "super-secret-value" not in str(settings)

    def test_a_secret_does_not_appear_in_the_credentials_repr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENA_CLIENT_ID", "athena-id")
        monkeypatch.setenv("ATHENA_CLIENT_SECRET", "super-secret-value")

        credentials = Settings().credentials_for(EHRType.ATHENA)

        assert "super-secret-value" not in repr(credentials)
        assert credentials.client_secret is not None
        assert credentials.client_secret.get_secret_value() == "super-secret-value"


class TestLaunchScopes:
    """The one thing that differs between the two SMART launch types."""

    def test_an_ehr_launch_appends_the_launch_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMART_SCOPES", "openid fhirUser")

        assert Settings().authorization_scopes(ehr_launch=True) == "openid fhirUser launch"

    def test_a_standalone_launch_appends_launch_patient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SMART_SCOPES", "openid fhirUser")

        assert Settings().authorization_scopes(ehr_launch=False) == "openid fhirUser launch/patient"


def test_the_launch_ttl_default_matches_the_documented_ten_minutes() -> None:
    assert Settings().smart_launch_ttl_seconds == 600
