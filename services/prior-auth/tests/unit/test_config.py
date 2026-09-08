"""Settings come from the environment, and every one of them is read."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import prior_auth
from prior_auth import patient_context
from prior_auth.config import (
    DEFAULT_FHIR_INTEGRATION_TIMEOUT_SECONDS,
    DEFAULT_FHIR_INTEGRATION_URL,
    DEFAULT_NOTE_RETRY_DELAYS,
    Settings,
    get_settings,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestDefaults:
    def test_an_unset_environment_still_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("REDIS_URL", "FHIR_INTEGRATION_URL", "PRIOR_AUTH_NOTE_RETRY_DELAYS"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("NOTE_RETRY_DELAYS", raising=False)

        settings = Settings()

        assert settings.fhir_integration_url == DEFAULT_FHIR_INTEGRATION_URL
        assert settings.fhir_integration_timeout_seconds == (
            DEFAULT_FHIR_INTEGRATION_TIMEOUT_SECONDS
        )
        assert settings.note_retry_delays == DEFAULT_NOTE_RETRY_DELAYS

    def test_the_retry_schedule_is_the_task_s_three_attempts(self) -> None:
        """TASK-060 specifies 3 attempts at 2s/4s/8s — one more look than delays."""
        assert DEFAULT_NOTE_RETRY_DELAYS == (2.0, 4.0, 8.0)


class TestOverrides:
    def test_the_service_url_comes_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FHIR_INTEGRATION_URL", "http://fhir-integration:8004")
        assert get_settings().fhir_integration_url == "http://fhir-integration:8004"

    def test_the_retry_schedule_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deployment observing slower generations widens it without a release."""
        monkeypatch.setenv("NOTE_RETRY_DELAYS", "[1,2]")
        assert get_settings().note_retry_delays == (1.0, 2.0)

    def test_a_non_positive_timeout_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FHIR_INTEGRATION_TIMEOUT_SECONDS", "0")
        with pytest.raises(ValueError, match="greater than 0"):
            get_settings()

    def test_settings_are_read_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FHIR_INTEGRATION_URL", "http://one:8004")
        first = get_settings()
        monkeypatch.setenv("FHIR_INTEGRATION_URL", "http://two:8004")
        assert get_settings() is first


class TestEverySettingIsRead:
    """A setting bound by nothing looks configured and is not.

    CLAUDE.md names that trap explicitly, and this repository has found it three
    times — most recently ``FHIR_INTEGRATION_URL``, which sat in
    ``.env.example`` unread from TASK-001 until TASK-052b bound it.
    """

    def test_no_field_is_declared_without_a_reader(self) -> None:
        """Scans the whole package rather than a hand-listed set of modules.

        A list would need updating whenever a module is added, and the update
        that gets forgotten is exactly the one where a new setting went unread.
        """
        package = Path(inspect.getfile(prior_auth)).parent
        readers = "".join(
            source.read_text(encoding="utf-8")
            for source in package.rglob("*.py")
            if source.name != "config.py"
        )
        unread = [
            name
            for name in Settings.model_fields
            if f".{name}" not in readers and f'"{name}"' not in readers
        ]
        assert unread == [], f"declared but read by nothing: {unread}"


class TestTheSettingsAreActuallyUsed:
    async def test_the_configured_timeout_reaches_the_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the behaviour follows the setting rather than the literal."""
        monkeypatch.setenv("FHIR_INTEGRATION_TIMEOUT_SECONDS", "3.5")
        seen: list[Any] = []
        original = patient_context.httpx.AsyncClient

        def build(*args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs.get("timeout"))
            kwargs["transport"] = patient_context.httpx.MockTransport(
                lambda _r: patient_context.httpx.Response(200, json={"data": {}, "error": None})
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(patient_context.httpx, "AsyncClient", build)

        await patient_context.fetch_patient_context(launch_id="l", patient_id="p")

        assert seen == [3.5]
