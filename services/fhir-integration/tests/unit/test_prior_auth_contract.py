"""The submission-method vocabulary has one definition, proven not assumed (TASK-054).

``track-a-clinical`` owns ``prior_auth_requests`` and therefore owns
``SubmissionMethod``; ``src/adapters/models.py`` mirrors it so this service can
type what it submitted without importing across a service boundary. The reasoning
for mirroring rather than importing is in ``test_note_contract.py`` and is the
same here.

What makes the mirror safe is this file. A member added, renamed or respelled on
the owning side and not here would not fail anywhere else: this service would
send a string that side's validator refuses, and the symptom would be a rejected
write *after* a payer had already accepted the submission — the one ordering
where the wreckage is a real prior authorization nothing here has a record of.

``.github/scripts/detect-changed-members.sh`` selects this service when
``track-a-clinical`` changes, so an edit on the owning side re-runs this file
rather than leaving it decorative.
"""

from __future__ import annotations

import uuid

import pytest

from src.adapters.models import SubmissionMethod as Mirror
from track_a_clinical.models import PriorAuthRequest
from track_a_clinical.models import SubmissionMethod as Owner


def test_the_two_definitions_have_the_same_members() -> None:
    """Same names, so neither side can add a method the other cannot spell."""
    assert {method.name for method in Mirror} == {method.name for method in Owner}


def test_the_two_definitions_have_the_same_values() -> None:
    """Same wire values, which is what the ``VARCHAR(50)`` actually stores.

    Matching names with drifting values is the worse failure of the two: it type
    checks on both sides and only diverges in the database.
    """
    assert {method.value for method in Mirror} == {method.value for method in Owner}


@pytest.mark.parametrize("method", list(Mirror))
def test_every_mirrored_method_is_accepted_by_the_owning_validator(method: Mirror) -> None:
    """The owning side's write validator accepts everything this side can send.

    Asserted by writing the value onto the real mapped class, not by round-
    tripping the owning enum. The validator is what a submission recorded over
    HTTP actually meets, and it is the thing that would reject a drifted value.
    """
    request = PriorAuthRequest(encounter_id=uuid.uuid4(), submission_method=method.value)

    assert request.submission_method == method.value
