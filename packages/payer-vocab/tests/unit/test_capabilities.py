"""Which payers are expected to expose the Da Vinci APIs.

The behavioural cases moved here from ``track-b-rag``'s ``TestIsCrdSupported``
when TASK-061 made this package the one definition of the set. They are the same
assertions; what changed is that they now guard the symbol both consumers
actually call, rather than one consumer's private copy.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from payer_vocab import (
    CMS_0057_PAYERS,
    normalize_payer,
    supports_crd,
    supports_prior_auth_api,
)

#: Both predicates answer the same mandate question today, so every case below
#: is asserted against each. If they are ever split — one payer publishing CRD
#: and not PAS — this parametrisation is what will fail first and force the
#: cases apart deliberately rather than by omission.
PREDICATES: tuple[Callable[[str], bool], ...] = (supports_crd, supports_prior_auth_api)


@pytest.mark.parametrize("predicate", PREDICATES)
class TestMandateCoverage:
    """Who is inside CMS-0057-F and who is not."""

    def test_mandated_payers_are_covered(self, predicate: Callable[[str], bool]) -> None:
        assert predicate("medicare-advantage")
        assert predicate("medicaid")

    def test_commercial_payers_are_not(self, predicate: Callable[[str], bool]) -> None:
        """The bulk of what private practices see is outside the mandate.

        For track-b-rag that means the RAG path answers alone; for prior-auth it
        means the submission needs a person. Neither is an error.
        """
        for payer in ("aetna", "bcbs-ma", "cigna", "unitedhealthcare", "anthem-bcbs"):
            assert not predicate(payer)

    def test_traditional_medicare_is_not_medicare_advantage(
        self, predicate: Callable[[str], bool]
    ) -> None:
        """The distinction the payer vocabulary preserves for exactly this decision.

        Traditional Medicare's rules come from CMS policy text we ingest;
        Advantage plans set their own and are inside the mandate. Collapsing the
        two slugs would route one down the other's path.
        """
        assert not predicate("cms-medicare")
        assert predicate("medicare-advantage")

    def test_a_display_name_does_not_match(self, predicate: Callable[[str], bool]) -> None:
        """Only canonical slugs — the failure this package exists to prevent.

        A caller that forgets to normalise gets ``False``, which is
        indistinguishable from a payer genuinely outside the mandate. That is
        why the predicates do not normalise their own argument: doing so would
        hide the call site that forgot.
        """
        assert not predicate("Medicare Advantage")
        assert not predicate("MEDICAID")

    def test_the_display_name_matches_once_normalised(
        self, predicate: Callable[[str], bool]
    ) -> None:
        """And the fix is one function call, at the caller."""
        assert predicate(normalize_payer("Medicare Advantage"))


class TestOneDefinition:
    """The set itself, and the property that made it worth extracting."""

    def test_every_member_is_a_canonical_slug(self) -> None:
        """A member that is not its own normalisation could never be matched."""
        for slug in CMS_0057_PAYERS:
            assert normalize_payer(slug) == slug

    def test_both_predicates_read_the_same_set(self) -> None:
        """No second definition — the point of the extraction.

        Asserted over the union of the set and a sample from outside it, so a
        future divergence has to be introduced deliberately rather than by one
        predicate quietly growing its own membership test.
        """
        for payer in CMS_0057_PAYERS | {"aetna", "cms-medicare", "unknown-payer"}:
            assert supports_crd(payer) == supports_prior_auth_api(payer)
