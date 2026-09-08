"""The two HTTP calls a submission passes through. TASK-061.

Driven through ``httpx.MockTransport`` rather than by patching the functions, so
what is under test is the request that actually goes on the wire and the mapping
from a real response onto this module's exceptions. Patching would assert only
that the caller calls what the test defined.

The distinctions asserted here are the ones the route's status codes rest on: a
request that does not exist is not an outage, a refusal is not a timeout, and a
timeout is not a failure — the payer may have taken the request in.
"""

from __future__ import annotations

import httpx
import pytest

from prior_auth import submission_client

REQUEST_ID = "0b1d9a2e-6f43-4c1a-9d21-4e8b7f0c5a33"
LAUNCH_ID = "launch-2f9c"
TRACK_A_URL = "http://track-a-clinical.test"
FHIR_URL = "http://fhir-integration.test"

ROUTING_BODY = {
    "data": {
        "request_id": REQUEST_ID,
        "status": "pending",
        "payer_name": "Medicare Advantage",
        "launch_id": LAUNCH_ID,
        "submittable": True,
    },
    "error": None,
}

SUBMISSION_BODY = {
    "data": {
        "request_id": REQUEST_ID,
        "outcome": "queued",
        "submission_method": "fhir-pas",
        "payer_reference_number": None,
    },
    "error": None,
}


class Recorder:
    """Answers with a fixed response and keeps what was asked."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def read(recorder: Recorder) -> submission_client.RoutingFacts:
    return await submission_client.read_routing_facts(
        REQUEST_ID,
        base_url=TRACK_A_URL,
        timeout_seconds=1.0,
        client=recorder.client(),
    )


async def submit(recorder: Recorder) -> submission_client.SubmissionResult:
    return await submission_client.submit_through_fhir_integration(
        REQUEST_ID,
        launch_id=LAUNCH_ID,
        base_url=FHIR_URL,
        timeout_seconds=1.0,
        client=recorder.client(),
    )


class TestReadingRoutingFacts:
    async def test_it_reads_the_narrow_route(self) -> None:
        """The whole point of the read: the payload with no clinical content.

        Asking for ``/prior-auth/{id}`` instead would pull note excerpts across
        the network for a decision that ignores them, and write a
        ``READ_PRIOR_AUTH`` row for an access that read no clinical content.
        """
        recorder = Recorder(httpx.Response(200, json=ROUTING_BODY))

        facts = await read(recorder)

        assert recorder.requests[0].url.path == f"/prior-auth/{REQUEST_ID}/routing"
        assert facts.payer_name == "Medicare Advantage"
        assert facts.launch_id == LAUNCH_ID
        assert facts.submittable is True

    async def test_a_404_is_a_not_found_rather_than_an_outage(self) -> None:
        """Different facts about different systems, and different status codes.

        A request that does not exist will not start existing on a retry.
        """
        recorder = Recorder(httpx.Response(404, json={"data": None, "error": {"code": "x"}}))

        with pytest.raises(submission_client.RoutingReadError) as caught:
            await read(recorder)

        assert caught.value.not_found is True
        assert caught.value.timed_out is False

    async def test_a_timeout_is_marked_as_one(self) -> None:
        recorder = Recorder(httpx.TimeoutException("slow"))

        with pytest.raises(submission_client.RoutingReadError) as caught:
            await read(recorder)

        assert caught.value.timed_out is True
        assert caught.value.not_found is False

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(500, json={"data": None, "error": {"code": "x"}}),
            httpx.Response(200, content=b"not json"),
            httpx.Response(200, json={"data": {"status": "pending"}, "error": None}),
        ],
        ids=["server-error", "unparseable", "missing-required-field"],
    )
    async def test_an_unusable_answer_is_a_read_error(self, response: httpx.Response) -> None:
        """A body that does not match the model is as unusable as no body."""
        with pytest.raises(submission_client.RoutingReadError) as caught:
            await read(Recorder(response))

        assert caught.value.not_found is False

    async def test_a_trailing_slash_on_the_base_url_does_not_double(self) -> None:
        recorder = Recorder(httpx.Response(200, json=ROUTING_BODY))

        await submission_client.read_routing_facts(
            REQUEST_ID,
            base_url=TRACK_A_URL + "/",
            timeout_seconds=1.0,
            client=recorder.client(),
        )

        assert "//prior-auth" not in str(recorder.requests[0].url)


class TestSubmitting:
    async def test_the_body_is_the_request_id_and_the_launch_is_a_header(self) -> None:
        """The launch is a credential handle: a header, never the URL.

        And the body carries an identifier rather than the bundle, so a payer
        submission's payload is never under the caller's control.
        """
        recorder = Recorder(httpx.Response(201, json=SUBMISSION_BODY))

        await submit(recorder)

        request = recorder.requests[0]
        assert request.url.path == "/fhir/prior-auth"
        assert request.headers[submission_client.LAUNCH_ID_HEADER] == LAUNCH_ID
        assert LAUNCH_ID not in str(request.url)
        assert request.read() == b'{"request_id":"%s"}' % REQUEST_ID.encode()

    async def test_a_queued_answer_with_no_reference_is_carried(self) -> None:
        """Ordinary and conformant — and the state a reference is most often
        absent in, so treating that absence as a failure would misreport it."""
        result = await submit(Recorder(httpx.Response(201, json=SUBMISSION_BODY)))

        assert result.outcome == "queued"
        assert result.payer_reference_number is None
        assert result.submission_method == "fhir-pas"

    async def test_a_refusal_carries_the_code_the_router_branches_on(self) -> None:
        recorder = Recorder(
            httpx.Response(
                422,
                json={
                    "data": None,
                    "error": {
                        "code": submission_client.ERROR_CODE_PATH_NOT_CONFIGURED,
                        "message": "not configured",
                    },
                },
            )
        )

        with pytest.raises(submission_client.SubmissionRefused) as caught:
            await submit(recorder)

        assert caught.value.code == submission_client.ERROR_CODE_PATH_NOT_CONFIGURED
        assert caught.value.status_code == 422

    async def test_a_refusal_with_an_unreadable_body_still_refuses(self) -> None:
        """Without a code the router cannot branch, so it must not treat the
        refusal as an unconfigured path — the one branch that would record a
        manual outcome for a request that was actually rejected."""
        recorder = Recorder(httpx.Response(422, content=b"<html>"))

        with pytest.raises(submission_client.SubmissionRefused) as caught:
            await submit(recorder)

        assert caught.value.code == ""

    async def test_a_5xx_is_unavailable_rather_than_a_refusal(self) -> None:
        """A refusal is about this request; a 5xx is about that service."""
        with pytest.raises(submission_client.SubmissionUnavailable) as caught:
            await submit(Recorder(httpx.Response(502, json={"data": None, "error": None})))

        assert caught.value.timed_out is False

    async def test_a_timeout_is_marked_and_never_retried_here(self) -> None:
        """The ambiguity that makes the route say "reconcile" rather than "retry".

        The payer may have taken the request in, so a second submission asks it
        to open a second review. Nothing in this module retries.
        """
        recorder = Recorder(httpx.TimeoutException("slow"))

        with pytest.raises(submission_client.SubmissionUnavailable) as caught:
            await submit(recorder)

        assert caught.value.timed_out is True
        assert len(recorder.requests) == 1


class TestTheBorrowedClient:
    async def test_an_injected_client_is_not_closed(self) -> None:
        """A caller's client outlives the call, so a second one still works."""
        recorder = Recorder(httpx.Response(200, json=ROUTING_BODY))
        client = recorder.client()

        await submission_client.read_routing_facts(
            REQUEST_ID, base_url=TRACK_A_URL, timeout_seconds=1.0, client=client
        )

        assert not client.is_closed

    async def test_no_credential_or_body_reaches_a_log_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing here logs a launch id or a response body.

        The launch is a credential handle, and that service's messages can name
        a payer reference. Errors carry fixed descriptions this module wrote.
        """
        recorder = Recorder(
            httpx.Response(422, json={"data": None, "error": {"code": "X", "message": "secret"}})
        )

        with caplog.at_level("DEBUG"), pytest.raises(submission_client.SubmissionRefused):
            await submit(recorder)

        assert LAUNCH_ID not in caplog.text
        assert "secret" not in caplog.text


def test_the_routing_path_names_a_route_that_exists() -> None:
    """A path formatted here and served there is a contract with no compiler.

    This service already depends on ``track-a-clinical`` for the shared mapped
    classes, so the real route table is importable and the check can be made
    against it rather than against a copy of the string.
    ``detect-changed-members.sh`` re-runs this job whenever that service's
    ``src/`` changes, so a rename over there fails here rather than at runtime —
    the same arrangement as ``fhir-integration``'s contract test on the two
    shared vocabularies.

    The *router* is imported rather than the application: building that app
    constructs its settings, which require a JWT signing key this service has no
    business holding. The router carries the paths, which is the whole contract
    under test.
    """
    from track_a_clinical.api.prior_auth import router as owning_router

    assert submission_client.ROUTING_PATH in {route.path for route in owning_router.routes}


def test_the_formatted_routing_path_is_what_goes_on_the_wire() -> None:
    """The template is a FastAPI path; formatting it must produce a real URL."""
    formatted = submission_client.ROUTING_PATH.format(request_id=REQUEST_ID)

    assert formatted == f"/prior-auth/{REQUEST_ID}/routing"
    assert "{" not in formatted


def test_the_submission_path_and_header_match_the_fhir_routes() -> None:
    """``fhir-integration`` is not a dependency of this service, so this pins the
    spelling rather than checking it — and the reason it is asserted at all is
    that nothing else on this side would notice a rename."""
    assert submission_client.SUBMIT_PATH == "/fhir/prior-auth"
    assert submission_client.LAUNCH_ID_HEADER == "X-MedAuth-Launch-Id"
