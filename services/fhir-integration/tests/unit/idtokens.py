"""Minting SMART ``id_token``s and the key set that verifies them.

Shared by the ``smart.identity`` unit tests and the callback tests, so both
exercise a real RSA signature rather than a stubbed verifier. Signing with a
real key is the point: a fake that returned "verified" would assert nothing
about the check TASK-051c exists to perform, and the failing-signature test in
particular is only meaningful against a genuine one.

The key pair is generated once per test session — 2048-bit RSA generation is
not free, and nothing here depends on a fresh key per test.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://auth.example-hospital.org"
JWKS_URI = "https://auth.example-hospital.org/.well-known/jwks.json"
AUDIENCE = "medauth-generic-client"
KEY_ID = "test-signing-key-1"

#: The Practitioner an EHR says authorized the launch, relative as a real
#: ``fhirUser`` claim often is.
PRACTITIONER_CLAIM = "Practitioner/prov-77"


@lru_cache(maxsize=2)
def _key(tag: str) -> rsa.RSAPrivateKey:
    """A cached RSA key. ``tag`` lets a test get a *different*, wrong key."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(*, kid: str = KEY_ID, tag: str = "primary") -> dict[str, Any]:
    """The public key set an EHR would publish, as JWKS JSON."""
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_key(tag).public_key(), as_dict=True)
    public_jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": [public_jwk]}


def id_token(
    *,
    fhir_user: str | None = PRACTITIONER_CLAIM,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    kid: str | None = KEY_ID,
    tag: str = "primary",
    expires_in: int = 300,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint an ``id_token`` the way an EHR's authorization server would.

    Every parameter exists so one test can make exactly one thing wrong — a
    different signing key, a foreign audience, an expired token — while the rest
    of the token stays valid.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": "provider-subject-identifier",
        "iat": now,
        "exp": now + expires_in,
    }
    if fhir_user is not None:
        claims["fhirUser"] = fhir_user
    if extra_claims:
        claims.update(extra_claims)

    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, _key(tag), algorithm="RS256", headers=headers)
