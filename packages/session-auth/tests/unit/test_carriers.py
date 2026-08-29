"""Which carrier the token came from, and what the handshake echoes back.

These prove the two carriers are genuinely interchangeable rather than one of
them having quietly weaker checks — the failure this package's design is meant
to make impossible.
"""

from __future__ import annotations

import pytest

from session_auth import (
    JWT_SUBPROTOCOL_PREFIX,
    SESSION_SUBPROTOCOL,
    WS_CLOSE_UNAUTHORIZED,
    SessionAuthError,
    extract_token,
    select_subprotocol,
)


class TestExtraction:
    """Which carrier the token came from, and what counts as absent."""

    def test_the_header_carrier_is_read(self) -> None:
        assert extract_token(authorization="Bearer abc.def.ghi", subprotocols=[]) == "abc.def.ghi"

    def test_the_bearer_scheme_is_matched_case_insensitively(self) -> None:
        """RFC 7235 makes the scheme case-insensitive and real clients vary."""
        assert extract_token(authorization="bearer abc", subprotocols=[]) == "abc"
        assert extract_token(authorization="BEARER abc", subprotocols=[]) == "abc"

    def test_the_subprotocol_carrier_is_read(self) -> None:
        offered = [SESSION_SUBPROTOCOL, f"{JWT_SUBPROTOCOL_PREFIX}abc.def.ghi"]

        assert extract_token(authorization=None, subprotocols=offered) == "abc.def.ghi"

    def test_the_header_wins_when_a_client_sends_both(self) -> None:
        """Not a security property — just a rule, so the behaviour is defined."""
        token = extract_token(
            authorization="Bearer from-header",
            subprotocols=[f"{JWT_SUBPROTOCOL_PREFIX}from-subprotocol"],
        )

        assert token == "from-header"

    def test_the_subprotocol_carrier_is_used_when_the_header_is_unusable(self) -> None:
        """A header without the scheme is not a token, so the other carrier answers."""
        token = extract_token(
            authorization="Basic dXNlcjpwYXNz",
            subprotocols=[f"{JWT_SUBPROTOCOL_PREFIX}from-subprotocol"],
        )

        assert token == "from-subprotocol"

    @pytest.mark.parametrize(
        ("authorization", "subprotocols"),
        [
            (None, []),
            ("", []),
            ("Bearer ", []),
            ("Bearer", []),
            (None, [SESSION_SUBPROTOCOL]),
            (None, [JWT_SUBPROTOCOL_PREFIX]),
            (None, ["something.else"]),
        ],
    )
    def test_nothing_usable_is_an_auth_error(
        self,
        authorization: str | None,
        subprotocols: list[str],
    ) -> None:
        with pytest.raises(SessionAuthError) as refusal:
            extract_token(authorization=authorization, subprotocols=subprotocols)

        assert refusal.value.reason == "missing_token"


class TestSubprotocolSelection:
    """What the handshake echoes back, and what it must never echo."""

    def test_the_version_marker_is_selected(self) -> None:
        offered = [SESSION_SUBPROTOCOL, f"{JWT_SUBPROTOCOL_PREFIX}secret-token"]

        assert select_subprotocol(offered) == SESSION_SUBPROTOCOL

    def test_the_token_entry_is_never_selected(self) -> None:
        """Selecting it would copy a live credential into the response headers."""
        selected = select_subprotocol([f"{JWT_SUBPROTOCOL_PREFIX}secret-token"])

        assert selected is None
        assert selected != f"{JWT_SUBPROTOCOL_PREFIX}secret-token"

    def test_a_client_that_offered_nothing_is_answered_with_nothing(self) -> None:
        assert select_subprotocol([]) is None


def test_the_refusal_close_code_is_in_the_application_range() -> None:
    """4000-4999 is what RFC 6455 leaves to applications; 4401 echoes HTTP 401.

    Pinned because two services now close with it and a client distinguishes a
    refused token from an ordinary close by this number alone.
    """
    assert WS_CLOSE_UNAUTHORIZED == 4401
    assert 4000 <= WS_CLOSE_UNAUTHORIZED <= 4999
