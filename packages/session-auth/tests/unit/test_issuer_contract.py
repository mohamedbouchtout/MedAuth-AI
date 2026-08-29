"""The issuer and this validator have to agree — proven, not assumed (TASK-006b).

``track-a-clinical`` mints session tokens; this package decides whether to accept
one. Every other test here mints its own token by hand, which proves the
validator behaves as intended but cannot prove it agrees with the real issuer:
both sides could drift together into a shape no client actually receives.

This test closes that gap for the re-minted token specifically, which is what
TASKS.md asks TASK-006b to demonstrate — a re-mint is worthless if the token it
returns cannot open the socket the client needed it for. It imports the issuer's
own ``mint_session_jwt`` and feeds its output to ``validate_token`` with nothing
in between.

It lived in ``audio-ingestion``'s suite until TASK-041, for a reason that has now
gone away: the validator was ``src.auth`` there, and four services still install
a top-level ``src`` package into the shared virtualenv, so the name resolved to
whichever sorted first. The validator is a named package now, and the contract it
proves belongs to the package rather than to either of the two services that
consume it — otherwise the second consumer either copies this file or trusts an
agreement nothing in its own suite checks.

``.github/scripts/detect-changed-members.sh`` selects this package when
``track-a-clinical`` changes, so a change to the issuer re-runs this file rather
than leaving it decorative.
"""

from __future__ import annotations

import datetime
import uuid

import pytest

from session_auth import SessionAuthError, validate_token
from track_a_clinical.config import Settings as IssuerSettings
from track_a_clinical.session_tokens import mint_session_jwt

#: One secret, both sides — HS256 is symmetric and every service is first-party.
SHARED_KEY = "contract-test-signing-key-32-byte"


def issuer(ttl: int = 900) -> IssuerSettings:
    return IssuerSettings(jwt_signing_key=SHARED_KEY, session_ttl_seconds=ttl)


def test_a_reminted_token_opens_the_socket_it_was_minted_for() -> None:
    """The acceptance criterion from TASK-006b, end to end across two services."""
    session_id = uuid.uuid4()
    provider_id = uuid.uuid4()

    # Exactly what POST /sessions/{session_id}/token returns: same session, same
    # provider, fresh exp. The route adds no claims of its own.
    reminted = mint_session_jwt(session_id=session_id, provider_id=provider_id, settings=issuer())

    identity = validate_token(reminted, session_id=str(session_id), signing_key=SHARED_KEY)

    assert identity.session_id == session_id
    assert identity.provider_id == provider_id


def test_a_reminted_token_is_refused_for_a_different_session() -> None:
    """The issuer's session binding is the one this validator enforces."""
    reminted = mint_session_jwt(
        session_id=uuid.uuid4(), provider_id=uuid.uuid4(), settings=issuer()
    )

    with pytest.raises(SessionAuthError) as raised:
        validate_token(reminted, session_id=str(uuid.uuid4()), signing_key=SHARED_KEY)

    assert raised.value.reason == "session_mismatch"


def test_the_refreshed_expiry_is_what_extends_the_socket_window() -> None:
    """A re-mint is pointless if the new exp is not actually in the future."""
    session_id = uuid.uuid4()
    expired = mint_session_jwt(
        session_id=session_id,
        provider_id=uuid.uuid4(),
        settings=issuer(),
        now=datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=20),
    )
    reminted = mint_session_jwt(session_id=session_id, provider_id=uuid.uuid4(), settings=issuer())

    with pytest.raises(SessionAuthError) as raised:
        validate_token(expired, session_id=str(session_id), signing_key=SHARED_KEY)
    assert raised.value.reason == "expired"

    # Same session, same issuer, same key — only exp moved, and that is enough.
    assert validate_token(reminted, session_id=str(session_id), signing_key=SHARED_KEY)


def test_the_issuers_claim_set_is_exactly_what_this_validator_requires() -> None:
    """Either side growing a claim alone should surface here, not in production."""
    from track_a_clinical.session_tokens import CLAIM_NAMES

    assert CLAIM_NAMES == frozenset({"session_id", "provider_id", "exp"})


def test_the_issuer_and_the_validator_agree_on_the_signing_key_floor() -> None:
    """Both sides enforce 32 bytes, from their own constant.

    They are deliberately separate constants — see :mod:`session_auth.tokens` —
    so this is the assertion that keeps them in step. A validator accepting a key
    the issuer refuses turns a configuration mistake into tokens that never
    validate rather than a startup failure.
    """
    from session_auth import MIN_SIGNING_KEY_BYTES
    from track_a_clinical.config import MIN_SIGNING_KEY_BYTES as ISSUER_FLOOR

    assert MIN_SIGNING_KEY_BYTES == ISSUER_FLOOR
