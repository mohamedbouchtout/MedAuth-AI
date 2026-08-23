"""The live check against the Da Vinci CRD Reference Implementation.

**Why this is gated.** It needs the CRD RI container from
``docker-compose.yml`` running. A pull request that touches nothing near this
service should not go red because a developer had not started it, and the RI
takes over a minute to compile its rule libraries before it answers.

**Why gating it is not the same as deleting it.** The fixtures in
``tests/fixtures/crd/`` were captured from this server on one particular day.
They are what :mod:`track_b_rag.crd` is tested against, so if the RI's response
shape moves — a renamed card type, a determination expressed somewhere new —
every unit test keeps passing against a picture of the past while the mapping
quietly stops working. This module is what notices. It runs nightly with
``RUN_CRD_LIVE_TESTS=1`` from ``.github/workflows/nightly-live-checks.yml``.

**Do not relax anything here to make a failure go away.** Each assertion is a
structural claim the mapping depends on. If one breaks, re-capture the fixtures
from the running server and fix the mapping to match — the server is right and
we are wrong.

Note what this deliberately does *not* assert: that the RI answers for any
particular CPT code. Its rule library is HCPCS/DME, so every CPT code in this
product's corpus gets a card that decides nothing, and the RAG path answers.
That is the honest behaviour and it is asserted below as such.
"""

from __future__ import annotations

import os

import httpx
import pytest

from track_b_rag import crd

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_CRD_LIVE_TESTS") != "1",
        reason="live CRD check; set RUN_CRD_LIVE_TESTS=1 (the nightly workflow does)",
    ),
]

BASE_URL = os.environ.get("CRD_BASE_URL", "http://localhost:8006")

#: HCPCS Level II, Non-Emergency Ambulance Transportation. The RI holds a rule
#: for it that returns a prior-authorization determination without needing any
#: demographics, which is what makes it answerable from Stage 1.
COVERED_HCPCS_CODE = "A0426"

#: CPT, total knee arthroplasty. A code from this product's own domain, and one
#: the RI's DME rule library holds nothing for.
UNCOVERED_CPT_CODE = "27447"

#: HCPCS Level II, Home Oxygen Therapy. Its rule keys on patient age, which
#: Stage 1 is not allowed to know.
DEMOGRAPHIC_HCPCS_CODE = "E0424"


async def test_the_discovery_endpoint_is_release_prefixed() -> None:
    """``/r4/cds-services``, not the ``/cds-services`` the RI's README documents.

    The path in :data:`track_b_rag.crd.CDS_SERVICES_PATH` came from the running
    server rather than its documentation. If the RI ever moves it back, this
    fails and names the reason.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{BASE_URL}{crd.CDS_SERVICES_PATH}")

    response.raise_for_status()
    services = {service["id"] for service in response.json()["services"]}
    assert crd.ORDER_SIGN_SERVICE in services


async def test_a_covered_code_yields_a_determination() -> None:
    """The whole tier, end to end, against a real payer server."""
    determination = await crd.determine(
        base_url=BASE_URL,
        timeout_seconds=30.0,
        procedure="non-emergency ambulance transport",
        cpt_code=COVERED_HCPCS_CODE,
        payer="medicare-advantage",
        plan_type="PPO",
        state="MA",
    )

    assert determination is not None
    assert determination.requires_auth is True


async def test_an_uncovered_code_decides_nothing() -> None:
    """Silence from the payer is not a "no authorization required".

    This is the assertion that keeps the tier safe: if a future RI change made
    an unknown code return something the mapping read as a negative
    determination, this service would start telling providers that
    unauthorized orders are clear.
    """
    determination = await crd.determine(
        base_url=BASE_URL,
        timeout_seconds=30.0,
        procedure="total knee arthroplasty",
        cpt_code=UNCOVERED_CPT_CODE,
        payer="medicare-advantage",
        plan_type="PPO",
        state="MA",
    )

    assert determination is None


async def test_a_rule_needing_demographics_decides_nothing() -> None:
    """Stage 1 sends no patient, so a rule that needs one cannot answer it.

    Asserted rather than worked around: the alternative is fabricating a birth
    date, which would produce a confident determination about a person who does
    not exist.
    """
    determination = await crd.determine(
        base_url=BASE_URL,
        timeout_seconds=30.0,
        procedure="home oxygen therapy",
        cpt_code=DEMOGRAPHIC_HCPCS_CODE,
        payer="medicare-advantage",
        plan_type="PPO",
        state="MA",
    )

    assert determination is None


async def test_the_captured_fixtures_still_match_what_the_server_sends() -> None:
    """The drift check the gated fixtures exist for.

    Compares the live response's shape against the captured one — the card type
    and the coverage-information slice names the mapping reads. Values are free
    to differ; the structure is what :mod:`track_b_rag.crd` depends on.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{BASE_URL}{crd.CDS_SERVICES_PATH}/{crd.ORDER_SIGN_SERVICE}",
            json=crd.build_hook_request(
                procedure="non-emergency ambulance transport",
                cpt_code=COVERED_HCPCS_CODE,
                plan_type="PPO",
                state="MA",
            ),
        )

    response.raise_for_status()
    cards = response.json()["cards"]
    assert cards, "the RI answered with no cards at all"

    card_types = {
        (card.get("source") or {}).get("topic", {}).get("code")
        for card in cards
        if isinstance(card.get("source"), dict)
    }
    assert "prior-auth" in card_types

    slices = {
        part["url"]
        for card in cards
        for extension in crd._coverage_information(card)
        for part in extension["extension"]
    }
    assert {"doc-needed", "questionnaire"} <= slices, (
        "the coverage-information extension no longer carries the slices the "
        "mapping was written against; re-capture tests/fixtures/crd/"
    )
