"""Reading a patient's chart context from ``fhir-integration``.

The service boundary itself is not stubbed out — the call goes over HTTP through
the real client onto a mock transport, which is what keeps the ``READ_PATIENT``
audit row that service's route writes on the only path to a chart read.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from prior_auth import patient_context
from prior_auth.config import get_settings

FULL_CONTEXT: dict[str, Any] = {
    "patient": {"id": "patient-1", "birth_date": "1970-01-01"},
    "coverage": {"payer": "Aetna", "plan_type": "PPO", "member_id": "W123"},
    "conditions": [],
    "requires_manual_confirmation": False,
}


class RecordedFHIRIntegration:
    """Stands in for ``fhir-integration``, recording what was asked of it."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transport = httpx.MockTransport(self.handler)
        original = httpx.AsyncClient

        def build(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(patient_context.httpx, "AsyncClient", build)


def responding(payload: dict[str, Any] | None, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"data": payload, "error": None})


@pytest.fixture(autouse=True)
def _settings() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestTheRequest:
    async def test_the_launch_id_travels_in_the_header_never_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A launch_id resolves to an EHR access token — a capability handle.

        This repository keeps that class of value out of URLs that
        intermediaries log.
        """
        fake = RecordedFHIRIntegration(responding(FULL_CONTEXT))
        fake.install(monkeypatch)

        await patient_context.fetch_patient_context(launch_id="launch-abc", patient_id="patient-1")

        request = fake.requests[0]
        assert request.headers["X-MedAuth-Launch-Id"] == "launch-abc"
        assert "launch-abc" not in str(request.url)

    async def test_it_calls_the_patient_context_route(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = RecordedFHIRIntegration(responding(FULL_CONTEXT))
        fake.install(monkeypatch)

        await patient_context.fetch_patient_context(launch_id="l", patient_id="patient-1")

        assert fake.requests[0].url.path == "/fhir/patient/patient-1/context"

    async def test_the_base_url_can_be_overridden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = RecordedFHIRIntegration(responding(FULL_CONTEXT))
        fake.install(monkeypatch)

        await patient_context.fetch_patient_context(
            launch_id="l", patient_id="p", base_url="http://elsewhere:9000/"
        )

        assert fake.requests[0].url.host == "elsewhere"


class TestTheAnswer:
    async def test_the_coverage_is_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        RecordedFHIRIntegration(responding(FULL_CONTEXT)).install(monkeypatch)

        context = await patient_context.fetch_patient_context(launch_id="l", patient_id="p")

        assert context is not None
        assert context.coverage is not None
        assert context.coverage.payer == "Aetna"

    async def test_unknown_fields_do_not_break_the_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A local mirror with ``extra="ignore"``: that service may add fields."""
        payload = {**FULL_CONTEXT, "something_new": {"deeply": ["nested"]}}
        RecordedFHIRIntegration(responding(payload)).install(monkeypatch)

        assert await patient_context.fetch_patient_context(launch_id="l", patient_id="p")

    async def test_incomplete_coverage_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {**FULL_CONTEXT, "coverage": None, "requires_manual_confirmation": True}
        RecordedFHIRIntegration(responding(payload)).install(monkeypatch)

        context = await patient_context.fetch_patient_context(launch_id="l", patient_id="p")

        assert context is not None
        assert context.coverage is None
        assert context.requires_manual_confirmation is True


class TestFailure:
    @pytest.mark.parametrize("status_code", [401, 404, 500, 502, 504])
    async def test_a_non_2xx_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, status_code: int
    ) -> None:
        RecordedFHIRIntegration(responding(None, status_code=status_code)).install(monkeypatch)

        assert await patient_context.fetch_patient_context(launch_id="l", patient_id="p") is None

    async def test_a_transport_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        transport = httpx.MockTransport(explode)
        original = httpx.AsyncClient
        monkeypatch.setattr(
            patient_context.httpx,
            "AsyncClient",
            lambda *a, **kw: original(*a, **{**kw, "transport": transport}),
        )

        assert await patient_context.fetch_patient_context(launch_id="l", patient_id="p") is None

    async def test_a_body_that_is_not_the_envelope_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        RecordedFHIRIntegration(httpx.Response(200, json={"nope": True})).install(monkeypatch)

        assert await patient_context.fetch_patient_context(launch_id="l", patient_id="p") is None

    async def test_the_failure_is_logged_at_error_naming_the_consequence(
        self, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        """Nothing downstream reports this gap again, so this line is the record."""
        RecordedFHIRIntegration(responding(None, status_code=500)).install(monkeypatch)

        with caplog.at_level(logging.ERROR):
            await patient_context.fetch_patient_context(launch_id="launch-abc", patient_id="p")

        assert "no prior-authorization" in caplog.text
        # The credential never reaches a log line.
        assert "launch-abc" not in caplog.text
