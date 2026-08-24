"""Token validation, at the level below the route.

The route tests prove a bad token is refused. These prove *why* each one is bad,
and that the two carriers are genuinely interchangeable rather than one of them
having quietly weaker checks — which is the failure this module's design is
meant to make impossible.
"""

from __future__ import annotations

import datetime
import uuid

import jwt
import pytest

from src.auth import (
    JWT_SUBPROTOCOL_PREFIX,
    SESSION_SUBPROTOCOL,
    SessionAuthError,
    extract_token,
    select_subprotocol,
    validate_token,
)
from src.config import JWT_ALGORITHM, Settings

KEY = "auth-unit-test-signing-key-32-bytes"


def settings(key: str = KEY) -> Settings:
    return Settings(jwt_signing_key=key)


def mint(
    *,
    session_id: uuid.UUID,
    provider_id: uuid.UUID | None = None,
    key: str = KEY,
    lifetime_seconds: int = 900,
    claims: dict[str, object] | None = None,
) -> str:
    body: dict[str, object] = {
        "session_id": str(session_id),
        "provider_id": str(provider_id or uuid.uuid4()),
        "exp": int(
            (
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=lifetime_seconds)
            ).timestamp()
        ),
    }
    if claims is not None:
        body = claims
    return jwt.encode(body, key, algorithm=JWT_ALGORITHM)


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


class TestValidation:
    """The four checks, and the reason label each failure carries."""

    def test_a_good_token_yields_the_session_and_provider(self) -> None:
        session_id = uuid.uuid4()
        provider_id = uuid.uuid4()

        identity = validate_token(
            mint(session_id=session_id, provider_id=provider_id),
            session_id=str(session_id),
            settings=settings(),
        )

        assert identity.session_id == session_id
        assert identity.provider_id == provider_id

    def test_an_expired_token_is_refused(self) -> None:
        session_id = uuid.uuid4()

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=session_id, lifetime_seconds=-1),
                session_id=str(session_id),
                settings=settings(),
            )

        assert refusal.value.reason == "expired"

    def test_a_token_signed_with_another_key_is_refused(self) -> None:
        session_id = uuid.uuid4()

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=session_id, key="a-different-signing-key-32-bytes!"),
                session_id=str(session_id),
                settings=settings(),
            )

        assert refusal.value.reason == "invalid_token"

    def test_an_unsigned_token_is_refused(self) -> None:
        """``alg: none`` is the classic JWT bypass; the algorithm is pinned.

        This is why :mod:`src.config` fixes ``JWT_ALGORITHM`` rather than reading
        the header of the token being validated.
        """
        session_id = uuid.uuid4()
        unsigned = jwt.encode(
            {"session_id": str(session_id), "provider_id": str(uuid.uuid4()), "exp": 9_999_999_999},
            key="",
            algorithm="none",
        )

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(unsigned, session_id=str(session_id), settings=settings())

        assert refusal.value.reason == "invalid_token"

    def test_a_token_for_another_session_is_refused(self) -> None:
        """The check that stops one encounter's token opening another's socket."""
        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=uuid.uuid4()),
                session_id=str(uuid.uuid4()),
                settings=settings(),
            )

        assert refusal.value.reason == "session_mismatch"

    def test_the_session_claim_is_compared_as_a_uuid_not_as_text(self) -> None:
        """The same session in different capitalisation is the same session."""
        session_id = uuid.uuid4()

        identity = validate_token(
            mint(session_id=session_id),
            session_id=str(session_id).upper(),
            settings=settings(),
        )

        assert identity.session_id == session_id

    @pytest.mark.parametrize(
        "claims",
        [
            {"provider_id": str(uuid.uuid4()), "exp": 9_999_999_999},
            {"session_id": str(uuid.uuid4()), "exp": 9_999_999_999},
            {"session_id": str(uuid.uuid4()), "provider_id": str(uuid.uuid4())},
        ],
        ids=["no session_id", "no provider_id", "no exp"],
    )
    def test_a_token_missing_a_required_claim_is_refused(self, claims: dict[str, object]) -> None:
        """A token without ``exp`` is a token that never expires."""
        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=uuid.uuid4(), claims=claims),
                session_id=str(claims.get("session_id", uuid.uuid4())),
                settings=settings(),
            )

        assert refusal.value.reason == "missing_claim"

    def test_a_claim_that_is_not_a_uuid_is_refused(self) -> None:
        session_id = uuid.uuid4()
        claims = {
            "session_id": str(session_id),
            "provider_id": "not-a-uuid",
            "exp": 9_999_999_999,
        }

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=session_id, claims=claims),
                session_id=str(session_id),
                settings=settings(),
            )

        assert refusal.value.reason == "malformed_claim"

    def test_a_url_session_id_that_is_not_a_uuid_is_refused(self) -> None:
        session_id = uuid.uuid4()

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(
                mint(session_id=session_id),
                session_id="../../etc/passwd",
                settings=settings(),
            )

        assert refusal.value.reason == "malformed_session_id"

    def test_a_refusal_reason_never_carries_the_token(self) -> None:
        """The reason is logged. A token in it would be a credential in a log."""
        session_id = uuid.uuid4()
        token = mint(session_id=session_id, lifetime_seconds=-1)

        with pytest.raises(SessionAuthError) as refusal:
            validate_token(token, session_id=str(session_id), settings=settings())

        assert token not in str(refusal.value)
        assert token not in refusal.value.reason
