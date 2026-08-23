"""Payer slugging and alias resolution."""

from __future__ import annotations

import pytest

from payer_vocab import (
    KNOWN_PAYERS,
    PAYER_ALIASES,
    is_known_payer,
    normalize_payer,
    slugify_payer,
)


class TestSlugifyPayer:
    """The mechanical half — no curated knowledge involved."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Aetna", "aetna"),
            ("AETNA", "aetna"),
            ("  aetna  ", "aetna"),
            ("Aetna, Inc.", "aetna"),
            ("Aetna Health Inc", "aetna-health"),
            ("Blue Cross Blue Shield", "blue-cross-blue-shield"),
            ("Blue Cross & Blue Shield", "blue-cross-blue-shield"),
            ("UnitedHealthcare", "unitedhealthcare"),
            ("United  Healthcare", "united-healthcare"),
            ("Medicare Part B", "medicare-part-b"),
            ("Cigna Healthcare, LLC", "cigna-healthcare"),
        ],
    )
    def test_spellings_that_should_collapse(self, raw: str, expected: str) -> None:
        assert slugify_payer(raw) == expected

    def test_noise_only_name_keeps_its_words(self) -> None:
        """Reducing "The Group" to the empty string would lose the only identity it has."""
        assert slugify_payer("The Group") == "the-group"

    def test_name_without_alphanumerics_slugs_to_empty(self) -> None:
        assert slugify_payer("---") == ""


class TestNormalizePayer:
    """Slug plus alias resolution — what call sites actually use."""

    @pytest.mark.parametrize(
        "raw",
        [
            "Medicare",
            "MEDICARE",
            "Medicare Part A",
            "Medicare Part B",
            "medicare part b",
            "Original Medicare",
            "Traditional Medicare",
            "CMS",
            "Centers for Medicare & Medicaid Services",
        ],
    )
    def test_every_medicare_spelling_resolves_to_one_slug(self, raw: str) -> None:
        """The case this package was written for: CMS ingests, a Coverage queries."""
        assert normalize_payer(raw) == "cms-medicare"

    def test_medicare_advantage_is_not_traditional_medicare(self) -> None:
        """MA plans set their own rules on top of the national ones (TASK-015)."""
        assert normalize_payer("Medicare Advantage") == "medicare-advantage"
        assert normalize_payer("Medicare Part C") == "medicare-advantage"
        assert normalize_payer("Medicare Part C") != normalize_payer("Medicare Part B")

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Aetna", "AETNA, Inc."),
            ("Aetna", "CVS Aetna"),
            ("UnitedHealthcare", "United Healthcare"),
            ("UnitedHealthcare", "UHC"),
            ("BCBS", "Blue Cross Blue Shield"),
            ("Anthem", "Anthem Blue Cross Blue Shield"),
            ("Cigna", "Cigna Healthcare, LLC"),
        ],
    )
    def test_two_spellings_produce_one_slug(self, left: str, right: str) -> None:
        """One slug means one Qdrant filter value and one cache key, which is the point."""
        assert normalize_payer(left) == normalize_payer(right)

    def test_unknown_payer_normalizes_without_raising(self) -> None:
        """A payer we have never seen is not an error — it just retrieves nothing."""
        slug = normalize_payer("Sierra Valley Regional Health Plan")
        assert slug == "sierra-valley-regional-health-plan"
        assert is_known_payer(slug) is False

    def test_unknown_payer_is_still_stable_across_spellings(self) -> None:
        assert normalize_payer("sierra valley HEALTH") == normalize_payer("Sierra Valley Health")

    def test_empty_payer_raises(self) -> None:
        """An absent payer is a caller bug, unlike an unfamiliar one."""
        with pytest.raises(ValueError, match="no alphanumeric"):
            normalize_payer("   ")


class TestRealCoverageDisplays:
    """Names taken from live FHIR servers, not invented for the test.

    Sources, all fetched rather than recalled: the Oracle Health (Cerner) open
    sandbox at ``fhir-open.cerner.com``, the public HAPI R4 test server, and
    Synthea's own ``insurance_companies.csv`` — the roster the local dev HAPI
    server is seeded from in TASK-052. Every string below was observed in a
    ``Coverage.payor.display`` or in Synthea's payer table. They are here
    because guessing at plausible spellings is what let this class of bug
    through the first time.
    """

    @pytest.mark.parametrize(
        ("display", "expected"),
        [
            # Oracle Health open sandbox.
            ("Aetna", "aetna"),
            ("Blue Cross", "blue-cross-blue-shield"),
            ("Coventry Healthcare", "aetna"),
            ("Humana", "humana"),
            ("MEDICARE", "cms-medicare"),
            ("Medicaid", "medicaid"),
            ("United Healthcare", "unitedhealthcare"),
            ("Medicare Part A", "cms-medicare"),
            ("Medicare Part B", "cms-medicare"),
            ("Medi-Cal", "medicaid"),
            # Public HAPI R4 test server.
            ("Anthem Blue Cross Blue Shield", "anthem-bcbs"),
            ("Humana Medicare Advantage", "medicare-advantage"),
            ("Medicare", "cms-medicare"),
            ("UnitedHealthcare", "unitedhealthcare"),
            # Synthea's roster — what local dev data will actually contain.
            ("Blue Cross Blue Shield", "blue-cross-blue-shield"),
            ("Cigna Health", "cigna"),
            ("Anthem", "anthem-bcbs"),
        ],
    )
    def test_observed_display_resolves_to_a_curated_payer(
        self, display: str, expected: str
    ) -> None:
        slug = normalize_payer(display)
        assert slug == expected
        assert is_known_payer(slug) is True

    @pytest.mark.parametrize(
        "display",
        ["SELF PAY", "Government", "Dual Eligible"],
    )
    def test_non_payers_stay_unknown(self, display: str) -> None:
        """Observed in the same feeds, and deliberately not given a slug.

        None of these names a carrier whose prior-authorization policy we could
        ingest. Mapping them somewhere would manufacture a payer identity the
        source never asserted; leaving them unknown makes the query path log at
        WARNING, which is the accurate description of the situation.
        """
        assert is_known_payer(normalize_payer(display)) is False


class TestBlueCrossFamily:
    """Which Blue plan a name means is a question about whose policy governs.

    The 33 Association licensees publish their own prior-authorization
    criteria, so these slugs must not collapse: a merged slug would let one
    licensee's ingested policy answer a query about another, silently and
    wrongly. Keeping them apart fails the other way — an empty retrieval with a
    WARNING, which is the failure this package was written to make visible.
    """

    @pytest.mark.parametrize(
        ("display", "expected"),
        [
            ("Anthem", "anthem-bcbs"),
            ("Anthem BCBS", "anthem-bcbs"),
            ("Anthem Blue Cross", "anthem-bcbs"),
            ("Anthem Blue Cross Blue Shield", "anthem-bcbs"),
            ("Anthem Blue Cross and Blue Shield", "anthem-bcbs"),
        ],
    )
    def test_anthem_branded_names_resolve_to_anthem(self, display: str, expected: str) -> None:
        assert normalize_payer(display) == expected

    @pytest.mark.parametrize(
        "display",
        [
            "Blue Cross Blue Shield of Massachusetts",
            "Blue Cross and Blue Shield of Massachusetts, Inc.",
            "BCBS of Massachusetts",
            "BCBSMA",
        ],
    )
    def test_massachusetts_licensee_has_its_own_slug(self, display: str) -> None:
        """The pilot geography's licensee, so it is the first with a rule-2 slug."""
        assert normalize_payer(display) == "bcbs-ma"

    @pytest.mark.parametrize(
        "display",
        ["Blue Cross", "BCBS", "Blue Cross Blue Shield", "Blue Cross & Blue Shield"],
    )
    def test_unqualified_blue_names_land_in_the_generic_bucket(self, display: str) -> None:
        """Real EHR data names no licensee constantly; that is its own answer."""
        assert normalize_payer(display) == "blue-cross-blue-shield"

    def test_the_three_blue_slugs_stay_distinct(self) -> None:
        assert len({normalize_payer(n) for n in ("Anthem", "BCBSMA", "Blue Cross")}) == 3


class TestIsKnownPayer:
    def test_curated_payers_are_known(self) -> None:
        assert is_known_payer("cms-medicare") is True
        assert is_known_payer("aetna") is True

    def test_unseen_payer_is_not_known(self) -> None:
        assert is_known_payer("sierra-valley-health") is False

    def test_takes_a_slug_not_a_display_name(self) -> None:
        """Documented contract: callers pass normalize_payer()'s output."""
        assert is_known_payer("Medicare Part B") is False
        assert is_known_payer(normalize_payer("Medicare Part B")) is True


class TestAliasTableConsistency:
    def test_every_alias_target_is_a_known_payer(self) -> None:
        """An alias pointing at a slug nobody curated would warn on every query."""
        assert set(PAYER_ALIASES.values()) <= KNOWN_PAYERS

    def test_alias_keys_are_slugs(self) -> None:
        """Keyed on the slug, so casing and punctuation variants need no extra rows."""
        for alias in PAYER_ALIASES:
            assert slugify_payer(alias) == alias

    def test_alias_targets_are_fixed_points(self) -> None:
        """Resolution is one hop: normalizing a canonical slug returns it unchanged."""
        for target in set(PAYER_ALIASES.values()):
            assert normalize_payer(target) == target
