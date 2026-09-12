"""The queue cursor's encoding (TASK-072).

Pure functions, so they are tested here rather than in the DB-gated suite that
covers the query using them. What matters about this pair is that it round-trips
*both* halves of the sort key: a cursor carrying only the timestamp is the bug
the integration suite's tie case exists to catch, and it would be invisible from
the outside because such a cursor still looks opaque and still decodes.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from track_a_clinical import prior_auth

STARTED_AT = datetime.datetime(2026, 4, 1, 9, 0, tzinfo=datetime.UTC)


def test_a_cursor_round_trips_both_halves_of_the_sort_key() -> None:
    request_id = uuid.uuid4()

    decoded = prior_auth.decode_cursor(prior_auth.encode_cursor(STARTED_AT, request_id))

    assert decoded == (STARTED_AT, request_id)


def test_the_timezone_survives_the_round_trip() -> None:
    """An aware timestamp compared against a naive one is a comparison error, not a wrong page."""
    naive = prior_auth.decode_cursor(prior_auth.encode_cursor(STARTED_AT, uuid.uuid4()))[0]

    assert naive.tzinfo is not None
    assert naive.utcoffset() == datetime.timedelta(0)


def test_two_rows_sharing_a_timestamp_get_different_cursors() -> None:
    """The property the whole composite key exists for."""
    first = prior_auth.encode_cursor(STARTED_AT, uuid.uuid4())
    second = prior_auth.encode_cursor(STARTED_AT, uuid.uuid4())

    assert first != second


@pytest.mark.parametrize(
    "value",
    [
        "not-a-cursor",
        "",
        # Valid base64 of something that is not a cursor at all.
        "aGVsbG8gd29ybGQ=",
        # The right shape with an unparseable id half.
        "MjAyNi0wNC0wMVQwOTowMDowMCswMDowMHxub3QtYS11dWlk",
    ],
)
def test_a_value_this_service_did_not_issue_is_refused(value: str) -> None:
    """Refused rather than ignored.

    Silently restarting from the first page would make a broken pager look like
    a working one that had run out of rows — the same class of failure as an
    empty retrieval indistinguishable from a payer we hold no policy for.
    """
    with pytest.raises(prior_auth.InvalidCursor):
        prior_auth.decode_cursor(value)
