"""Turning a SMART ``id_token`` into an audit actor.

TASK-051c. TASK-051 requested the ``openid fhirUser`` scope and then stored
nothing from the ``id_token`` it got back, so every audit row this service wrote
had a null actor while the EHR had already told us who authorized the launch.
This module is the token-validation path that closes that — deliberately its own
task and its own module, because reading a claim responsibly means fetching a
key set and verifying a signature, which is an authentication mechanism rather
than a field to bolt onto a resource fetch (Known Constraints #8).

**Nothing here raises, and nothing here fails a launch.** Every failure returns
``None``: an unverifiable claim means the actor is unknown, which is exactly
what a null records. Falling back to the *unverified* value would be the
fabrication this task exists to remove, one step subtler than the
service-account UUID CLAUDE.md already refuses — it would look like a real
identity in the one table an auditor reads to answer "who accessed patient X".
A launch that cannot prove who started it is still a working launch, and
refusing it would trade a complete audit trail for no session at all.

**Nothing here logs the token, the claim, or the resolved reference.** A refusal
is logged as a fixed reason — the same rule ``packages/session-auth`` follows,
for the same reason: a log line is not a place to put a credential, and an
``id_token`` is one.
"""

from __future__ import annotations

import logging
from typing import Any, Final
from urllib.parse import urljoin

import httpx
import jwt
from jwt import PyJWKSet

logger = logging.getLogger(__name__)

#: How long to wait on an EHR's JWKS. Short for the same reason discovery's
#: timeout is: this runs inside a callback, which is a person waiting on a
#: browser redirect. A round default, not a measurement.
JWKS_TIMEOUT_SECONDS: Final = 10.0

#: The signature algorithms an ``id_token`` may use here. An allow-list rather
#: than "whatever the token's header says", which is the classic algorithm
#: confusion hole: without it a token could name ``none``, or name ``HS256`` and
#: invite the public key to be used as an HMAC secret. Asymmetric only —
#: OpenID Connect requires ``RS256`` support and vendors add EC variants.
SUPPORTED_ID_TOKEN_ALGORITHMS: Final = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
)

#: The only resource type accepted as an actor. ``fhirUser`` may reference a
#: ``Patient``, ``RelatedPerson`` or ``Person`` for patient-facing apps, and
#: this is not one: MedAuth launches from a clinician's chart view. The
#: restriction is not tidiness — ``audit_log.fhir_practitioner_ref`` is an actor
#: column, and writing a ``Patient`` reference into it would both misname what
#: the value is and put a patient identifier in a column nothing treats as one.
#: Anything else is refused and the actor stays unknown.
ACTOR_RESOURCE_TYPE: Final = "Practitioner"


def _jwks_key_for(jwks: dict[str, Any], id_token: str) -> Any | None:
    """Return the verification key for one token, or ``None`` if none matches.

    Selection is by ``kid``, which is what a key set is rotated through. A token
    with no ``kid`` is accepted only when the set holds exactly one key — with
    more than one there is no way to choose that is not a guess, and guessing at
    which key signed a credential is not verification.
    """
    key_set = PyJWKSet.from_dict(jwks)
    if not key_set.keys:
        return None

    kid = jwt.get_unverified_header(id_token).get("kid")
    if kid is None:
        return key_set.keys[0] if len(key_set.keys) == 1 else None

    for key in key_set.keys:
        if key.key_id == kid:
            return key
    return None


def _practitioner_reference(claim: str, fhir_base_url: str) -> str | None:
    """Return the ``fhirUser`` claim as an absolute ``Practitioner`` reference.

    Absolute, because a ``Practitioner`` id is unique only within one EHR:
    ``Practitioner/1`` on two servers is two different people, and storing the
    relative form would merge them into one audit identity. A claim that is
    already absolute is kept exactly as the EHR wrote it.

    Returns ``None`` for a reference to anything but a ``Practitioner`` — see
    ``ACTOR_RESOURCE_TYPE``.
    """
    reference = claim.strip()
    if not reference:
        return None

    # Works for both forms: an absolute URL ends ".../Practitioner/{id}" and a
    # relative reference is exactly "Practitioner/{id}".
    parts = reference.rstrip("/").split("/")
    if len(parts) < 2 or parts[-2] != ACTOR_RESOURCE_TYPE or not parts[-1]:
        return None

    if reference.startswith(("http://", "https://")):
        return reference
    return urljoin(f"{fhir_base_url.rstrip('/')}/", reference.lstrip("/"))


async def resolve_launch_actor(
    client: httpx.AsyncClient,
    *,
    id_token: str | None,
    jwks_uri: str | None,
    oidc_issuer: str | None,
    audience: str,
    fhir_base_url: str,
    issuer_host: str,
) -> str | None:
    """Verify an ``id_token`` and return its ``fhirUser`` as an audit actor.

    Args:
        client: The HTTP client to fetch the key set with.
        id_token: The ``id_token`` from the token exchange, if the EHR sent one.
        jwks_uri: Where the EHR publishes its signing keys, from the same SMART
            discovery document the launch's endpoints came from.
        oidc_issuer: The ``issuer`` from that document, checked against the
            token's ``iss``.
        audience: This deployment's ``client_id`` for the vendor, checked
            against the token's ``aud``. A token minted for a different client
            is not ours to read an identity out of.
        fhir_base_url: Used to absolutise a relative ``fhirUser``.
        issuer_host: The host, for log lines. Never the full ``iss``.

    Returns:
        An absolute ``Practitioner`` reference, or ``None`` when the EHR sent no
        ``id_token``, published no keys, or the token did not verify. ``None``
        is a complete answer — the caller records an unknown actor and carries
        on with the launch.
    """
    if id_token is None:
        logger.info("No id_token from %s — the launch's actor stays unknown.", issuer_host)
        return None

    if jwks_uri is None or oidc_issuer is None:
        # SMART's discovery document marks `issuer` and `jwks_uri` conditional,
        # so a server that does not support single sign-on legitimately omits
        # them. Without both there is nothing to verify against, and an
        # unverified claim is not written.
        logger.info(
            "No issuer or JWKS published by %s — the launch's actor stays unknown.",
            issuer_host,
        )
        return None

    try:
        response = await client.get(
            jwks_uri,
            headers={"Accept": "application/json"},
            timeout=JWKS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        jwks = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # str(exc) can carry the request URL; the class name and host are enough.
        logger.warning(
            "Could not read the key set from %s (%s) — actor stays unknown.",
            issuer_host,
            type(exc).__name__,
        )
        return None

    try:
        key = _jwks_key_for(jwks, id_token)
    except (jwt.PyJWTError, AttributeError, TypeError, ValueError) as exc:
        logger.warning(
            "Unusable key set from %s (%s) — actor stays unknown.",
            issuer_host,
            type(exc).__name__,
        )
        return None

    if key is None:
        logger.warning(
            "No key in %s's set matches the id_token — actor stays unknown.", issuer_host
        )
        return None

    try:
        claims = jwt.decode(
            id_token,
            key=key.key,
            algorithms=list(SUPPORTED_ID_TOKEN_ALGORITHMS),
            audience=audience,
            issuer=oidc_issuer,
            # No nonce check: a nonce defends against an id_token replayed
            # through the front channel, and this token arrived on the back
            # channel, in the response to a single-use authorization code that
            # was itself bound to this launch by the PKCE verifier.
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        # The reason is a class name, never the token or a claim from it.
        logger.warning(
            "id_token from %s did not verify (%s) — actor stays unknown.",
            issuer_host,
            type(exc).__name__,
        )
        return None

    claim = claims.get("fhirUser")
    if not isinstance(claim, str) or not claim.strip():
        # `profile` carried this in SMART 1.0 and is deliberately not read as a
        # fallback: this repository targets SMART on FHIR 2.0, and accepting an
        # older claim nobody has checked against a real server would be
        # inventing a source rather than reading one.
        logger.warning(
            "Verified id_token from %s carries no fhirUser — actor stays unknown.",
            issuer_host,
        )
        return None

    reference = _practitioner_reference(claim, fhir_base_url)
    if reference is None:
        logger.warning(
            "fhirUser from %s does not reference a %s — actor stays unknown.",
            issuer_host,
            ACTOR_RESOURCE_TYPE,
        )
        return None

    logger.info("Resolved the launch actor from %s's id_token.", issuer_host)
    return reference
