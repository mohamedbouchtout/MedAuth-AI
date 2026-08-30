"""Vendor detection and adapter selection.

Three of these tests are about failure modes rather than about the happy path,
and they are the reason the module is shaped the way it is: a vendor substring
appearing somewhere other than the host, an issuer from an EHR nobody has
integrated yet, and a vendor key that arrived as plain text from Redis.
"""

from __future__ import annotations

import logging

import pytest

from src.adapters.athena import AthenaAdapter
from src.adapters.base import EHRAdapter
from src.adapters.cerner import CernerAdapter
from src.adapters.ecw import ECWAdapter
from src.adapters.epic import EpicAdapter
from src.adapters.factory import EHRType, detect_ehr_from_issuer, get_adapter
from src.adapters.modmed import ModMedAdapter

VENDOR_ISSUERS = [
    ("https://api.platform.athenahealth.com/fhir/r4", EHRType.ATHENA),
    ("https://fhir.eclinicalworks.com/fhir/r4", EHRType.ECW),
    ("https://stage.ema-api.modmed.com/firm/fhir/r4", EHRType.MODMED),
    ("https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d", EHRType.CERNER),
    ("https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4", EHRType.EPIC),
]


@pytest.mark.parametrize(("iss_url", "expected"), VENDOR_ISSUERS)
def test_each_vendor_is_detected_from_its_issuer(iss_url: str, expected: EHRType) -> None:
    assert detect_ehr_from_issuer(iss_url) == expected


def test_detection_is_case_insensitive() -> None:
    assert detect_ehr_from_issuer("https://FHIR.EPIC.COM/api/FHIR/R4") == EHRType.EPIC


def test_a_port_and_credentials_do_not_defeat_detection() -> None:
    assert detect_ehr_from_issuer("https://user:pw@fhir.epic.com:8443/api") == EHRType.EPIC


def test_an_issuer_with_no_scheme_still_resolves_its_host() -> None:
    """Some EHRs send a bare host. The first path segment is still the host."""
    assert detect_ehr_from_issuer("fhir.epic.com/api/FHIR/R4") == EHRType.EPIC


def test_a_vendor_name_in_the_path_is_not_a_vendor() -> None:
    """The case host-only matching exists to prevent.

    "epic" is four characters that occur inside ordinary words, and a practice
    name reaches the path. Matching the whole URL would hand this launch the
    Epic adapter for a server that is not Epic.
    """
    iss = "https://fhir.example-health.org/epicenter-orthopedics/FHIR/R4"

    assert detect_ehr_from_issuer(iss) == EHRType.GENERIC


def test_a_vendor_name_in_the_query_string_is_not_a_vendor() -> None:
    iss = "https://fhir.example-health.org/r4?tenant=cerner"

    assert detect_ehr_from_issuer(iss) == EHRType.GENERIC


def test_oracle_health_resolves_to_cerner() -> None:
    """Both names appear in real issuer URLs while the rename is in progress."""
    assert detect_ehr_from_issuer("https://fhir.oraclehealth.com/r4") == EHRType.CERNER


def test_a_host_naming_both_cerner_and_oracle_resolves_to_one_vendor() -> None:
    """The ordering in _HOST_FRAGMENTS is fixed so this cannot be ambiguous."""
    assert detect_ehr_from_issuer("https://cerner.oraclehealth.com/r4") == EHRType.CERNER


def test_an_unknown_issuer_falls_back_to_generic_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown is not an error — the launch works and the mismatch is visible.

    Same arrangement as an unknown payer in payer-vocab. Raising here would turn
    every launch from an EHR we have not integrated into a failed launch.
    """
    with caplog.at_level(logging.WARNING):
        detected = detect_ehr_from_issuer("https://fhir.some-new-ehr.example/r4")

    assert detected == EHRType.GENERIC
    assert "fhir.some-new-ehr.example" in caplog.text


def test_the_warning_does_not_carry_the_query_string(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An iss can carry launch context. Only the host is logged."""
    with caplog.at_level(logging.WARNING):
        detect_ehr_from_issuer("https://fhir.unknown.example/r4?launch=abc123&tenant=xyz")

    assert "launch=abc123" not in caplog.text
    assert "abc123" not in caplog.text


def test_an_empty_issuer_is_generic_rather_than_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert detect_ehr_from_issuer("") == EHRType.GENERIC


EXPECTED_ADAPTERS: dict[EHRType, type[EHRAdapter]] = {
    EHRType.ATHENA: AthenaAdapter,
    EHRType.ECW: ECWAdapter,
    EHRType.MODMED: ModMedAdapter,
    EHRType.CERNER: CernerAdapter,
    EHRType.EPIC: EpicAdapter,
    EHRType.GENERIC: EHRAdapter,
}


@pytest.mark.parametrize(("ehr_type", "expected_class"), EXPECTED_ADAPTERS.items())
def test_every_vendor_key_has_an_adapter_behind_it(
    ehr_type: EHRType, expected_class: type[EHRAdapter]
) -> None:
    """A key added without a class would otherwise fail at a SMART launch."""
    adapter = get_adapter(ehr_type, "https://fhir.example.org/r4", "token")

    assert type(adapter) is expected_class


def test_the_case_list_above_covers_the_whole_vocabulary() -> None:
    """Guards the test above: a new EHRType member must be given a case.

    Without this, adding a member and forgetting the class behind it produces a
    green suite and a KeyError at a real SMART launch.
    """
    assert set(EXPECTED_ADAPTERS) == set(EHRType)


def test_a_vendor_key_read_back_from_redis_still_selects_an_adapter() -> None:
    """TASK-051 stores the key in Redis, which hands it back as plain text.

    A StrEnum member does not hash equal to its own text, so without the runtime
    coercion in get_adapter() this lookup would miss.
    """
    adapter = get_adapter("epic", "https://fhir.epic.com/r4", "token")  # type: ignore[arg-type]

    assert type(adapter) is EpicAdapter


def test_an_unknown_vendor_key_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="allscripts"):
        get_adapter("allscripts", "https://fhir.example.org/r4", "token")  # type: ignore[arg-type]


def test_detection_and_selection_compose() -> None:
    """The two functions a route is allowed to import, used the way a route uses them."""
    iss = "https://api.platform.athenahealth.com/fhir/r4"

    adapter = get_adapter(detect_ehr_from_issuer(iss), iss, "token")

    assert type(adapter) is AthenaAdapter
