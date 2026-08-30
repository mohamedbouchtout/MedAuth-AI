"""The base adapter's surface: what exists, what it refuses, and what it never prints.

Nothing here is implemented yet — TASK-052 through TASK-054 fill the methods in.
What these tests hold in place until then is the shape TASK-056 and TASK-057
override against, the fact that the class can be instantiated at all, and the
fact that an access token does not render.
"""

from __future__ import annotations

import inspect

import pytest

from src.adapters.athena import AthenaAdapter
from src.adapters.base import EHRAdapter
from src.adapters.cerner import CernerAdapter
from src.adapters.ecw import ECWAdapter
from src.adapters.epic import EpicAdapter
from src.adapters.modmed import ModMedAdapter

PRIMITIVES = ("get_patient", "get_coverage", "get_conditions", "get_encounter")
COMPOSED = ("get_patient_context",)
DEFERRED = ("write_clinical_note", "submit_prior_auth")

TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.not-a-real-token"

VENDOR_ADAPTERS = (AthenaAdapter, ECWAdapter, ModMedAdapter, CernerAdapter, EpicAdapter)


def _adapter() -> EHRAdapter:
    return EHRAdapter(fhir_base_url="https://fhir.example.org/r4", access_token=TOKEN)


def test_the_base_adapter_is_instantiable() -> None:
    """It is not abstract, and that is the whole reason the generic fallback works.

    An unrecognised issuer routes here (see detect_ehr_from_issuer). Marking the
    class abstract would turn every launch from an unknown EHR into a failure,
    which is the outcome the fallback exists to avoid.
    """
    assert isinstance(_adapter(), EHRAdapter)


@pytest.mark.parametrize("method_name", [*PRIMITIVES, *COMPOSED, *DEFERRED])
def test_every_method_is_async(method_name: str) -> None:
    """Async-first: a route handler awaits these, and a sync one would block the loop."""
    assert inspect.iscoroutinefunction(getattr(EHRAdapter, method_name))


@pytest.mark.parametrize("method_name", [*PRIMITIVES, *COMPOSED])
async def test_the_fetches_are_stubs_naming_the_task_that_implements_them(
    method_name: str,
) -> None:
    with pytest.raises(NotImplementedError, match="TASK-052"):
        await getattr(_adapter(), method_name)("patient-1")


async def test_the_note_write_back_is_a_stub() -> None:
    with pytest.raises(NotImplementedError, match="TASK-053"):
        await _adapter().write_clinical_note("encounter-1", "note text", ["M17.11"])


async def test_the_prior_auth_submission_is_a_stub() -> None:
    with pytest.raises(NotImplementedError, match="TASK-054"):
        await _adapter().submit_prior_auth(bundle=None)  # type: ignore[arg-type]


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
    adapter = adapter_class(fhir_base_url="https://fhir.example.org/r4", access_token=TOKEN)

    assert TOKEN not in repr(adapter)
    assert TOKEN not in str(adapter)
    assert "https://fhir.example.org/r4" in repr(adapter)
