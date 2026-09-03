"""The prior-auth vocabularies are closed and enforced on write (TASK-054).

The column is ``VARCHAR(50)`` and constrains nothing on its own, so the guarantee
that one method has one spelling lives in :class:`SubmissionMethod` and in the
validator on the mapped class. Two services write this column and one of them
gets its value over HTTP, where the type system has already stopped applying —
which is the case these tests are about.
"""

from __future__ import annotations

import uuid

import pytest

from track_a_clinical.models import (
    PRIOR_AUTH_STATUS_ERROR,
    PRIOR_AUTH_STATUS_SUBMITTED,
    PriorAuthRequest,
    SubmissionMethod,
    SubmissionOutcome,
)
from track_a_clinical.prior_auth import status_for_outcome


def make_request(**overrides: object) -> PriorAuthRequest:
    """A row with only what the mapped class needs to be constructed."""
    fields: dict[str, object] = {"encounter_id": uuid.uuid4()}
    fields.update(overrides)
    return PriorAuthRequest(**fields)


def test_every_member_is_accepted() -> None:
    """A member set on the column survives unchanged.

    Guards the round trip the enum exists for: what goes into the column is the
    member's own text, so nothing has to decode it on the way back out.
    """
    for method in SubmissionMethod:
        request = make_request(submission_method=method)
        assert request.submission_method == method.value
        assert isinstance(request.submission_method, str)


def test_the_plain_string_form_is_accepted() -> None:
    """The value as it arrives over HTTP, or back out of the database.

    ``fhir-integration`` records its submission through a route rather than a
    shared object, so what reaches this column is a string that happens to spell
    a member. Refusing it would make the enum unusable at the only boundary that
    matters.
    """
    request = make_request(submission_method="fhir-pas")
    assert request.submission_method == SubmissionMethod.FHIR_PAS


def test_null_means_not_yet_submitted() -> None:
    """NULL is a real state — a bundle assembled by TASK-060 and not yet sent."""
    assert make_request(submission_method=None).submission_method is None
    assert make_request().submission_method is None


@pytest.mark.parametrize(
    "value",
    [
        "FHIR_PAS",  # the member's Python name, not its value
        "fhir_pas",  # underscore where the vocabulary has a hyphen
        "FHIR-PAS",  # right spelling, wrong case
        "CoverMyMeds",  # the vendor's own capitalisation
        "carrier-pigeon",  # a method that does not exist
        "",  # the empty string, which a form post makes easy to send
    ],
)
def test_a_value_outside_the_vocabulary_is_refused(value: str) -> None:
    """Every near-miss raises rather than being stored or coerced.

    The first four are the failure this vocabulary exists to prevent: two
    spellings of one method, sitting in one column, matching neither each other
    nor anything a query asks for. Coercing them would hide exactly that.
    """
    with pytest.raises(ValueError, match="submission_method must be one of"):
        make_request(submission_method=value)


def test_the_refusal_names_what_is_permitted() -> None:
    """A rejected write says what would have been accepted.

    The reader of this error is someone wiring up TASK-061's router, and a bare
    "invalid value" would send them to the source to find out what is allowed.
    """
    with pytest.raises(ValueError) as excinfo:
        make_request(submission_method="carrier-pigeon")

    message = str(excinfo.value)
    assert "carrier-pigeon" in message
    for method in SubmissionMethod:
        assert method.value in message


class TestPayerOutcome:
    """``payer_outcome`` is the same arrangement one column over (TASK-054).

    It arrives over HTTP from ``fhir-integration`` exactly as the method does, is
    compared by string equality by whoever follows a queued request up, and sits
    in a ``VARCHAR(20)`` that constrains nothing. So it gets the same enum and the
    same validator, for the same reasons.
    """

    def test_every_member_is_accepted(self) -> None:
        for outcome in SubmissionOutcome:
            assert make_request(payer_outcome=outcome).payer_outcome == outcome.value

    def test_null_is_accepted(self) -> None:
        """A request that has not been submitted has no answer to hold."""
        assert make_request(payer_outcome=None).payer_outcome is None

    def test_an_unknown_outcome_raises_rather_than_being_stored(self) -> None:
        """An answer we cannot name is not one we can honestly record."""
        with pytest.raises(ValueError, match="payer_outcome must be one of"):
            make_request(payer_outcome="approved")

    def test_the_permitted_values_are_named_in_the_message(self) -> None:
        with pytest.raises(ValueError, match="complete, error, partial, queued"):
            make_request(payer_outcome="pending")

    def test_a_differently_spelled_member_is_refused(self) -> None:
        """Storing ``COMPLETE`` beside ``complete`` is what the enum exists to prevent."""
        with pytest.raises(ValueError):
            make_request(payer_outcome="COMPLETE")


class TestStatusForOutcome:
    """The lifecycle status a payer's answer puts a request into.

    The distinction that matters is ``error`` against everything else: the payer
    refused to process the request, so nothing is pending with them, and
    recording that as ``submitted`` would leave someone waiting for a decision
    that is never coming.
    """

    def test_a_refusal_is_not_a_submission(self) -> None:
        assert status_for_outcome(SubmissionOutcome.ERROR) == PRIOR_AUTH_STATUS_ERROR

    @pytest.mark.parametrize(
        "outcome",
        [SubmissionOutcome.COMPLETE, SubmissionOutcome.QUEUED, SubmissionOutcome.PARTIAL],
    )
    def test_everything_the_payer_took_in_is_submitted(self, outcome: SubmissionOutcome) -> None:
        """Which of the three it was stays on ``payer_outcome``, not here."""
        assert status_for_outcome(outcome) == PRIOR_AUTH_STATUS_SUBMITTED
