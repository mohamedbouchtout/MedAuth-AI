"""PKCE, as SMART on FHIR 2.0 requires it of every client.

CLAUDE.md pins SMART on FHIR 2.0, which requires PKCE of all clients —
confidential ones included, where it defends against an authorization code
intercepted between the authorization server and this service's callback. It is
not a hardening step to add once the flow works: a code exchanged without the
verifier that matches its challenge is a code anyone holding it can redeem.

``S256`` only. RFC 7636 also defines a ``plain`` method, where the challenge is
the verifier sent in clear on the authorization redirect — which is the thing
PKCE exists to avoid, so it is not offered here and nothing negotiates down to
it.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

#: The only method this service offers. Sent as ``code_challenge_method``.
CODE_CHALLENGE_METHOD = "S256"

#: Bytes of entropy behind a verifier. RFC 7636 requires the encoded verifier to
#: be 43-128 characters; 32 bytes base64url-encodes to exactly 43, the floor the
#: RFC sets and the length the spec's own worked example uses.
_VERIFIER_BYTES = 32


def _b64url(raw: bytes) -> str:
    """Base64url-encode without padding, as RFC 7636 specifies."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_code_verifier() -> str:
    """Return a fresh PKCE code verifier.

    Uses ``secrets`` rather than ``random``: the verifier is what stops an
    intercepted authorization code being redeemed, so a predictable one is worth
    no more than no PKCE at all.

    Returns:
        A 43-character base64url string, held under ``fhir_launch:{state}``
        until the callback exchanges the code.
    """
    return _b64url(secrets.token_bytes(_VERIFIER_BYTES))


def derive_code_challenge(code_verifier: str) -> str:
    """Return the S256 challenge for a verifier.

    Args:
        code_verifier: The verifier held for this launch.

    Returns:
        The base64url-encoded SHA-256 of the verifier's ASCII bytes — what goes
        on the authorization redirect. The verifier itself never does.
    """
    return _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
