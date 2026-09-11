"""Unit tests for the floor semantics.

These are about the function's behaviour on logger levels. What the floors
actually buy is in ``test_libraries_stay_quiet.py``, which runs the libraries
themselves — a test that only compared levels against the table would pass just
as happily if every number in it were wrong.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from logging_policy import LIBRARY_LOG_FLOORS, install_logging_policy


@pytest.fixture(autouse=True)
def restore_levels() -> Iterator[None]:
    """Put every touched logger back, since logging state is process-global.

    Without this the first test to install the policy would decide the levels for
    every test after it, including the ones asserting on an unconfigured logger.
    """
    saved = {name: logging.getLogger(name).level for name in LIBRARY_LOG_FLOORS}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


class TestTheFloors:
    def test_an_unconfigured_logger_is_raised_to_its_floor(self) -> None:
        for name in LIBRARY_LOG_FLOORS:
            logging.getLogger(name).setLevel(logging.NOTSET)

        install_logging_policy()

        assert {name: logging.getLogger(name).level for name in LIBRARY_LOG_FLOORS} == dict(
            LIBRARY_LOG_FLOORS
        )

    def test_a_logger_set_below_its_floor_is_raised(self) -> None:
        """The case that matters: DEBUG is where the libraries write PHI."""
        logging.getLogger("botocore").setLevel(logging.DEBUG)

        install_logging_policy()

        assert logging.getLogger("botocore").level == logging.INFO

    def test_a_logger_already_quieter_is_left_alone(self) -> None:
        """Raising only. Lowering one would be this package loosening a rule."""
        logging.getLogger("httpx").setLevel(logging.CRITICAL)

        install_logging_policy()

        assert logging.getLogger("httpx").level == logging.CRITICAL

    def test_it_is_idempotent(self) -> None:
        """Every service calls it per app, and a test suite builds many."""
        install_logging_policy()
        first = {name: logging.getLogger(name).level for name in LIBRARY_LOG_FLOORS}

        install_logging_policy()

        assert {name: logging.getLogger(name).level for name in LIBRARY_LOG_FLOORS} == first

    def test_it_touches_no_logger_outside_the_table(self) -> None:
        """Not the root logger, and nothing this repository owns.

        The scope note says this package configures third-party levels and
        nothing else. A policy that quietly raised the root logger would silence
        our own audit-adjacent INFO lines along with the libraries'.
        """
        root_level = logging.getLogger().level
        ours = logging.getLogger("track_a_clinical")
        ours.setLevel(logging.DEBUG)

        install_logging_policy()

        assert logging.getLogger().level == root_level
        assert ours.level == logging.DEBUG

    def test_the_table_cannot_be_edited_by_a_caller(self) -> None:
        """A per-service override is the thing this package exists to prevent."""
        with pytest.raises(TypeError):
            LIBRARY_LOG_FLOORS["httpx"] = logging.DEBUG  # type: ignore[index]


class TestTheFloorsSurviveARootLevelChange:
    def test_httpx_stays_quiet_under_a_debug_root(self) -> None:
        """The scenario the policy is actually for.

        Nothing configured ``httpx`` before this package, so it inherited the
        root level — which is why "someone turns the root logger up" was enough
        to write a patient identifier to stdout. A level on the library's own
        logger is what makes the root level irrelevant.
        """
        install_logging_policy()
        logging.getLogger().setLevel(logging.DEBUG)

        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
        assert not logging.getLogger("botocore.endpoint").isEnabledFor(logging.DEBUG)
        assert not logging.getLogger("urllib3.connectionpool").isEnabledFor(logging.DEBUG)

    def test_a_child_logger_inherits_the_floor(self) -> None:
        """The table names families, not every module inside one.

        ``botocore.endpoint`` and ``botocore.parsers`` are where the request and
        response bodies go, and neither is listed: flooring ``botocore`` covers
        both, and covers the module the next botocore version adds.
        """
        install_logging_policy()

        for child in ("botocore.endpoint", "botocore.parsers", "httpcore.http11"):
            assert logging.getLogger(child).getEffectiveLevel() >= logging.INFO
