"""The base adapter's surface: what exists, what it refuses, and what it never prints.

The fetches themselves are exercised in ``test_fetches.py``; what this module
holds in place is the shape TASK-056 and TASK-057 override against, the fact that
the class can be instantiated at all, and the fact that an access token does not
render.

**The "these are stubs" tests were deliberately removed here as each stub was
filled, not left to pass by coincidence.** TASK-050 asserted every fetch raised
``NotImplementedError`` naming TASK-052, and TASK-052 removed those; TASK-053 and
TASK-054 did the same for ``write_clinical_note`` and ``submit_prior_auth`` in
turn. Nothing on this class is a stub any more, and a test asserting otherwise
would now be asserting the opposite of the truth.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

from src.adapters.athena import AthenaAdapter
from src.adapters.base import EHRAdapter
from src.adapters.cerner import CernerAdapter
from src.adapters.ecw import ECWAdapter
from src.adapters.epic import EpicAdapter
from src.adapters.models import PriorAuthContent
from src.adapters.modmed import ModMedAdapter
from src.adapters.pas_bundle import PriorAuthNotSubmittable

PRIMITIVES = ("get_patient", "get_coverage", "get_conditions", "get_encounter")
COMPOSED = ("get_patient_context",)
#: Neither layer: a write to a chart (TASK-053) and a submission to a payer
#: (TASK-054). Both implemented; they are grouped because neither composes a
#: primitive nor fetches one, which is what the two layers above describe.
OUTBOUND = ("write_clinical_note", "submit_prior_auth")

TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.not-a-real-token"

VENDOR_ADAPTERS = (AthenaAdapter, ECWAdapter, ModMedAdapter, CernerAdapter, EpicAdapter)


def _client() -> httpx.AsyncClient:
    """A client whose transport is never reached — these tests make no request."""
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))


def _adapter() -> EHRAdapter:
    return EHRAdapter(
        fhir_base_url="https://fhir.example.org/r4",
        access_token=TOKEN,
        http_client=_client(),
    )


def test_the_base_adapter_is_instantiable() -> None:
    """It is not abstract, and that is the whole reason the generic fallback works.

    An unrecognised issuer routes here (see detect_ehr_from_issuer). Marking the
    class abstract would turn every launch from an unknown EHR into a failure,
    which is the outcome the fallback exists to avoid.
    """
    assert isinstance(_adapter(), EHRAdapter)


@pytest.mark.parametrize("method_name", [*PRIMITIVES, *COMPOSED, *OUTBOUND])
def test_every_method_is_async(method_name: str) -> None:
    """Async-first: a route handler awaits these, and a sync one would block the loop."""
    assert inspect.iscoroutinefunction(getattr(EHRAdapter, method_name))


async def test_the_prior_auth_submission_refuses_a_request_with_no_procedure() -> None:
    """No longer a stub (TASK-054), and it refuses before any payer is called.

    The parameter is ``content``, not ``bundle``, and it is normalized content
    rather than a FHIR ``Claim``. Both halves of that were corrected against
    ``OperationDefinition/Claim-submit`` — see the method's own docstring.

    A request naming no procedure has nothing to seek authorization for. The
    refusal comes out of the builder, so it happens before the submission rather
    than as a rejection from the payer — and the transport here is one that
    would fail loudly if it were reached at all.
    """
    content = PriorAuthContent(
        request_id="request-1",
        patient_id="patient-7",
        encounter_id="encounter-4",
    )
    with pytest.raises(PriorAuthNotSubmittable, match="no procedure"):
        await _adapter().submit_prior_auth(content)


@pytest.mark.parametrize("adapter_class", VENDOR_ADAPTERS)
def test_every_vendor_adapter_extends_the_base(adapter_class: type[EHRAdapter]) -> None:
    """A subclass that stopped inheriting would silently lose the standard FHIR path."""
    assert issubclass(adapter_class, EHRAdapter)


@pytest.mark.parametrize("adapter_class", [EHRAdapter, *VENDOR_ADAPTERS])
def test_no_adapter_renders_its_access_token(adapter_class: type[EHRAdapter]) -> None:
    """The token is a credential, and an adapter reaches log lines it must not.

    repr() is where an object leaks into a traceback, an f-string, or a logging
    call that interpolated it lazily. Covering every subclass means one that
    adds its own __repr__ has to keep this true.
    """
    adapter = adapter_class(
        fhir_base_url="https://fhir.example.org/r4",
        access_token=TOKEN,
        http_client=_client(),
    )

    assert TOKEN not in repr(adapter)
    assert TOKEN not in str(adapter)
    assert "https://fhir.example.org/r4" in repr(adapter)
