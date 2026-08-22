"""Jurisdiction code normalisation, including CMS's non-USPS codes."""

from __future__ import annotations

import pytest

from payer_vocab import (
    CMS_JURISDICTION_CODES,
    USPS_STATE_CODES,
    normalize_state,
    normalize_states,
)


class TestNormalizeState:
    @pytest.mark.parametrize(("raw", "expected"), [("MA", "MA"), ("ma", "MA"), (" ma ", "MA")])
    def test_usps_codes_pass_through(self, raw: str, expected: str) -> None:
        assert normalize_state(raw) == expected

    @pytest.mark.parametrize(
        ("cms_code", "expected"),
        [
            ("DN", "NY"),  # New York - Downstate
            ("QN", "NY"),  # New York - Queens
            ("UN", "NY"),  # New York - Upstate
            ("NF", "CA"),  # California - Northern
            ("SF", "CA"),  # California - Southern
            ("EM", "MO"),  # Missouri - Northeastern & Southern
            ("WM", "MO"),  # Missouri - Northwestern
        ],
    )
    def test_sub_state_jurisdictions_collapse_to_their_state(
        self, cms_code: str, expected: str
    ) -> None:
        """A Coverage resource says NY; CMS says DN, QN or UN for the same state."""
        assert normalize_state(cms_code) == expected

    def test_cnmi_maps_to_its_usps_code(self) -> None:
        """Four characters at the source, and the column it lands in is CHAR(2)."""
        assert normalize_state("CNMI") == "MP"

    @pytest.mark.parametrize("territory", ["AS", "GU", "PR", "VI"])
    def test_territories_with_usps_codes_pass_through(self, territory: str) -> None:
        assert normalize_state(territory) == territory

    @pytest.mark.parametrize("bad", ["XX", "Massachusetts", "", "  "])
    def test_unrecognised_code_raises(self, bad: str) -> None:
        """Silently storing an unmatched code looks like an empty corpus at query time."""
        with pytest.raises(ValueError, match="Unrecognised state"):
            normalize_state(bad)

    def test_every_cms_code_maps_into_the_usps_set(self) -> None:
        for code, target in CMS_JURISDICTION_CODES.items():
            assert target in USPS_STATE_CODES, code
            assert len(target) == 2, code


class TestNormalizeStates:
    def test_returns_sorted_distinct_codes(self) -> None:
        assert normalize_states(["ME", "MA", "ny"]) == ["MA", "ME", "NY"]

    def test_two_jurisdictions_in_one_state_collapse_to_one_entry(self) -> None:
        """DN, QN and UN are all New York — the list is of states, not jurisdictions."""
        assert normalize_states(["DN", "QN", "UN"]) == ["NY"]

    def test_ordering_is_stable_regardless_of_input_order(self) -> None:
        """A jurisdiction that has not changed must produce the same list every scrape."""
        assert normalize_states(["VT", "MA", "CT"]) == normalize_states(["CT", "VT", "MA"])

    def test_empty_jurisdiction_returns_empty_list(self) -> None:
        assert normalize_states([]) == []

    def test_one_bad_code_fails_the_whole_jurisdiction(self) -> None:
        with pytest.raises(ValueError, match="Unrecognised state"):
            normalize_states(["MA", "XX"])
