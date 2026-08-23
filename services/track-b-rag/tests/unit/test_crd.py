"""The Da Vinci CRD tier, tested against responses a real payer server produced.

Every fixture under ``tests/fixtures/crd/`` was captured from the HL7 CRD
Reference Implementation running from ``docker-compose.yml``, by posting the
request :func:`track_b_rag.crd.build_hook_request` builds and saving what came
back. That is deliberate and is the same requirement TASK-011 and TASK-013 hold
their parsers to: a hand-written CDS Hooks fixture can only encode what its
author already believed the response looked like, which is exactly the belief
under test. Writing these by hand would have missed that the RI states its
determination in the card *type* and never emits the IG's ``pa-needed`` slice.

The single exception is :func:`test_conformant_pa_needed_is_read`, which is
built from the IG's ``ext-coverage-information`` StructureDefinition rather than
captured, because no implementation we can reach emits that slice yet. It is
labelled as such where it appears — it describes the standard, not an
observation, and if a real payer ever contradicts it, the payer is right.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from track_b_rag import crd

FIXTURES = Path(__file__).parent.parent / "fixtures" / "crd"


def load(name: str) -> dict[str, Any]:
    """Return a captured CRD Reference Implementation response."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class TestIsCrdSupported:
    """Which payers the tier is tried for."""

    def test_mandated_payers_are_supported(self) -> None:
        assert crd.is_crd_supported("medicare-advantage")
        assert crd.is_crd_supported("medicaid")

    def test_commercial_payers_are_not(self) -> None:
        """The bulk of what private practices see stays on the RAG path."""
        for payer in ("aetna", "bcbs-ma", "cigna", "unitedhealthcare", "anthem-bcbs"):
            assert not crd.is_crd_supported(payer)

    def test_traditional_medicare_is_not_medicare_advantage(self) -> None:
        """The distinction payer-vocab preserves specifically for this decision.

        Traditional Medicare's rules come from CMS policy text we ingest;
        Advantage plans set their own and answer over CRD. Collapsing the two
        slugs would route one down the other's path.
        """
        assert not crd.is_crd_supported("cms-medicare")
        assert crd.is_crd_supported("medicare-advantage")

    def test_a_display_name_does_not_match(self) -> None:
        """Only canonical slugs, for the reason packages/payer-vocab exists."""
        assert not crd.is_crd_supported("Medicare Advantage")
        assert not crd.is_crd_supported("MEDICAID")


class TestCodeSystem:
    """A payer matches on the code system as well as the code."""

    def test_hcpcs_level_two_codes(self) -> None:
        assert crd.code_system("A0426") == crd.HCPCS_SYSTEM
        assert crd.code_system("E0424") == crd.HCPCS_SYSTEM

    def test_cpt_codes(self) -> None:
        assert crd.code_system("27447") == crd.CPT_SYSTEM
        assert crd.code_system("70551") == crd.CPT_SYSTEM


class TestBuildHookRequest:
    """What goes to the payer — and, more importantly, what does not."""

    @pytest.fixture
    def request_body(self) -> dict[str, Any]:
        return crd.build_hook_request(
            procedure="knee MRI",
            cpt_code="73721",
            plan_type="PPO",
            state="MA",
        )

    def test_is_an_order_sign_hook(self, request_body: dict[str, Any]) -> None:
        assert request_body["hook"] == "order-sign"

    def test_carries_the_code_and_its_system(self, request_body: dict[str, Any]) -> None:
        order = request_body["context"]["draftOrders"]["entry"][0]["resource"]
        coding = order["code"]["coding"][0]
        assert coding["code"] == "73721"
        assert coding["system"] == crd.CPT_SYSTEM

    def test_carries_plan_type_and_state(self, request_body: dict[str, Any]) -> None:
        coverage = next(
            entry["resource"]
            for entry in request_body["prefetch"]["serviceRequestBundle"]["entry"]
            if entry["resource"]["resourceType"] == "Coverage"
        )
        assert coverage["class"][0]["value"] == "PPO"
        assert coverage["extension"][0]["valueString"] == "MA"

    def test_carries_no_patient_demographics(self, request_body: dict[str, Any]) -> None:
        """Stage 1 holds none, so none can be sent.

        A fabricated birth date or sex would make a payer rule that keys on
        either return a confident determination about a person who does not
        exist. The cost is that such a rule cannot answer us at all, which
        :meth:`TestReadDetermination.test_unable_to_process_decides_nothing`
        shows the tier handling by standing aside.
        """
        patient = next(
            entry["resource"]
            for entry in request_body["prefetch"]["serviceRequestBundle"]["entry"]
            if entry["resource"]["resourceType"] == "Patient"
        )
        assert set(patient) == {"resourceType", "id"}
        assert "birthDate" not in patient
        assert "gender" not in patient

    def test_hook_instance_is_not_derived_from_the_session(self) -> None:
        """This request leaves the cluster; it carries no identifier of ours."""
        first = crd.build_hook_request(
            procedure="knee MRI", cpt_code="73721", plan_type="PPO", state="MA"
        )
        second = crd.build_hook_request(
            procedure="knee MRI", cpt_code="73721", plan_type="PPO", state="MA"
        )
        assert first["hookInstance"] != second["hookInstance"]


class TestReadDetermination:
    """Mapping real payer responses onto a prior authorization answer."""

    def test_prior_auth_card_means_authorization_required(self) -> None:
        """The Reference Implementation's dialect: the answer is the card type.

        Captured for HCPCS A0426, Non-Emergency Ambulance Transportation.
        """
        determination = crd.read_determination(load("order_sign_prior_auth"))
        assert determination is not None
        assert determination.requires_auth is True
        assert determination.signal == "card-type:prior-auth"

    def test_documentation_only_decides_nothing(self) -> None:
        """A ``dtr-clin`` card says documentation is needed, not that auth is.

        Captured for HCPCS E0250. The card carries ``doc-needed`` and a
        questionnaire canonical and says nothing about prior authorization, so
        the RAG path answers that question instead of this tier guessing.
        """
        assert crd.read_determination(load("order_sign_documentation_only")) is None

    def test_unable_to_process_decides_nothing(self) -> None:
        """What a rule needing demographics returns to a patient-free request.

        Captured for HCPCS E0424, Home Oxygen Therapy, whose rule keys on age.
        """
        assert crd.read_determination(load("order_sign_unable_to_process")) is None

    def test_unknown_code_decides_nothing(self) -> None:
        """A code the payer holds no rule for is not a "no authorization needed".

        Captured for CPT 27447, total knee arthroplasty — a code from this
        product's actual domain, and one the RI's DME rule library knows
        nothing about. Reading this as "no auth required" would tell a provider
        an unauthorized order is clear, which is the one direction TASK-012
        forbids failing in.
        """
        assert crd.read_determination(load("order_sign_unknown_code")) is None

    def test_conformant_pa_needed_is_read(self) -> None:
        """The IG's dialect, which the Reference Implementation never emits.

        Constructed from the ``ext-coverage-information`` StructureDefinition
        rather than captured, for want of an implementation that produces it.
        Every other case in this class is a real response.
        """
        response = _pa_needed_response("auth-needed")
        determination = crd.read_determination(response)
        assert determination is not None
        assert determination.requires_auth is True
        assert determination.signal == "pa-needed:auth-needed"

    @pytest.mark.parametrize("code", ["auth-needed", "performpa", "conditional"])
    def test_codes_meaning_authorization_is_required(self, code: str) -> None:
        """``conditional`` counts as required: a maybe is not a no."""
        determination = crd.read_determination(_pa_needed_response(code))
        assert determination is not None and determination.requires_auth is True

    @pytest.mark.parametrize("code", ["no-auth", "satisfied"])
    def test_codes_meaning_it_is_not(self, code: str) -> None:
        determination = crd.read_determination(_pa_needed_response(code))
        assert determination is not None and determination.requires_auth is False

    def test_indeterminate_decides_nothing(self) -> None:
        """The payer saying it does not know is not the payer saying no."""
        assert crd.read_determination(_pa_needed_response("indeterminate")) is None

    def test_pa_needed_wins_over_card_type(self) -> None:
        """A conformant payer's explicit statement beats the inferred signal."""
        response = _pa_needed_response("no-auth")
        response["cards"][0]["source"] = {"topic": {"code": "prior-auth", "system": "irrelevant"}}
        determination = crd.read_determination(response)
        assert determination is not None
        assert determination.requires_auth is False

    def test_pa_needed_given_as_a_coding_is_read(self) -> None:
        """The slice is typed ``code``, but a payer sending a Coding is not wrong.

        The Reference Implementation expresses its own ``coverageInfo`` slice
        that way, so a payer doing the same for ``pa-needed`` is the likelier
        deviation to meet than any other.
        """
        response = _pa_needed_response("auth-needed")
        (extension,) = crd._coverage_information(response["cards"][0])
        slice_ = next(part for part in extension["extension"] if part["url"] == "pa-needed")
        del slice_["valueCode"]
        slice_["valueCoding"] = {"code": "auth-needed"}

        determination = crd.read_determination(response)

        assert determination is not None and determination.requires_auth is True

    def test_a_card_whose_topic_is_not_a_mapping_decides_nothing(self) -> None:
        assert crd.read_determination({"cards": [{"source": {"topic": "prior-auth"}}]}) is None

    def test_empty_card_list_decides_nothing(self) -> None:
        assert crd.read_determination({"cards": []}) is None

    @pytest.mark.parametrize(
        "response",
        [
            {},
            {"cards": None},
            {"cards": "not a list"},
            {"cards": [None, "text"]},
            {"cards": [{"suggestions": "not a list"}]},
            {"cards": [{"suggestions": [{"actions": [{"resource": None}]}]}]},
            {"cards": [{"source": "not a mapping"}]},
            {"cards": [{"source": {"topic": {"code": 42}}}]},
        ],
    )
    def test_malformed_responses_decide_nothing_rather_than_raising(
        self, response: dict[str, Any]
    ) -> None:
        """A payer's response is third-party JSON and gets no benefit of the doubt."""
        assert crd.read_determination(response) is None


class TestDetermine:
    """The call itself. Every failure is the same answer: let RAG decide."""

    async def _determine_against(
        self, handler: object, **overrides: Any
    ) -> crd.CrdDetermination | None:
        transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
        original = httpx.AsyncClient

        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        kwargs: dict[str, Any] = {
            "base_url": "http://crd.test",
            "timeout_seconds": 1.0,
            "procedure": "ambulance transport",
            "cpt_code": "A0426",
            "payer": "medicare-advantage",
            "plan_type": "PPO",
            "state": "MA",
        }
        kwargs.update(overrides)
        httpx.AsyncClient = client  # type: ignore[misc]
        try:
            return await crd.determine(**kwargs)
        finally:
            httpx.AsyncClient = original  # type: ignore[misc]

    async def test_returns_the_determination_on_a_real_answer(self) -> None:
        captured = load("order_sign_prior_auth")
        determination = await self._determine_against(
            lambda request: httpx.Response(200, json=captured)
        )
        assert determination is not None
        assert determination.requires_auth is True

    async def test_posts_to_the_release_prefixed_path(self) -> None:
        """``/r4/cds-services/...``, taken from the running server.

        The CRD RI's README documents ``/cds-services``; the server answers 404
        there because its controller prefixes every route with the FHIR release.
        """
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"cards": []})

        await self._determine_against(handler)
        assert seen == ["http://crd.test/r4/cds-services/order-sign-crd"]

    async def test_a_timeout_is_not_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        assert await self._determine_against(handler) is None

    async def test_a_transport_error_is_not_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        assert await self._determine_against(handler) is None

    @pytest.mark.parametrize("status", [400, 404, 500, 503])
    async def test_a_failing_status_decides_nothing(self, status: int) -> None:
        """Observed: the RI answers 500 to some request shapes it cannot handle."""
        assert (
            await self._determine_against(
                lambda request: httpx.Response(status, json={"error": "nope"})
            )
            is None
        )

    async def test_a_non_json_body_decides_nothing(self) -> None:
        assert (
            await self._determine_against(
                lambda request: httpx.Response(200, text="<html>maintenance</html>")
            )
            is None
        )

    async def test_a_non_object_body_decides_nothing(self) -> None:
        assert (
            await self._determine_against(lambda request: httpx.Response(200, json=[1, 2])) is None
        )


def _pa_needed_response(code: str) -> dict[str, Any]:
    """Return a response stating `code` in the IG's ``pa-needed`` slice.

    Constructed from the ``ext-coverage-information`` StructureDefinition. See
    this module's docstring for why this one shape is not captured from a
    running server.
    """
    return {
        "cards": [
            {
                "summary": "Coverage requirements",
                "indicator": "info",
                "suggestions": [
                    {
                        "label": "Save Update To EHR",
                        "actions": [
                            {
                                "type": "update",
                                "resource": {
                                    "resourceType": "ServiceRequest",
                                    "extension": [
                                        {
                                            "url": crd.COVERAGE_INFORMATION_URL,
                                            "extension": [
                                                {
                                                    "url": "covered",
                                                    "valueCode": "covered",
                                                },
                                                {
                                                    "url": "pa-needed",
                                                    "valueCode": code,
                                                },
                                            ],
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
