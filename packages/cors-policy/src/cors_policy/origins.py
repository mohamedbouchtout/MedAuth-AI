"""Parsing and matching the configured browser origins.

One list serves two consumers with different mechanics: FastAPI's
``CORSMiddleware`` on the HTTP routes (:mod:`cors_policy.middleware`) and the
``Origin`` check the WebSocket endpoints run for themselves, because a browser
applies no CORS to an upgrade request. Both call :func:`is_allowed_origin`, so
neither can drift into its own idea of what the configured list means.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

#: The refusal label a WebSocket endpoint logs when :func:`is_allowed_origin`
#: says no. A fixed string, matching ``session_auth``'s convention: a log line
#: names why a connection was refused and never what was presented.
ORIGIN_REFUSED_REASON: Final = "origin_not_allowed"


class CorsPolicyError(ValueError):
    """Raised when the configured origins cannot be used as a policy."""


def parse_allowed_origins(raw: str) -> tuple[str, ...]:
    """Parse ``CORS_ALLOWED_ORIGINS`` into the list the policy is built from.

    Comma-separated, whitespace around entries ignored, empty entries dropped so
    a trailing comma is not an origin. An empty value yields an empty tuple,
    which is a service that answers no browser — not a service that answers
    every browser.

    ``*`` is rejected outright rather than passed through. CLAUDE.md forbids it
    on a service that answers with PHI, and every service that installs this
    package answers with PHI; enforcing it here rather than at each call site
    puts the constraint inside the primitive, the same arrangement as
    ``api-envelope``'s validation handler never echoing a rejected value.

    :raises CorsPolicyError: if any entry is ``*``.
    """
    origins = tuple(entry.strip() for entry in raw.split(",") if entry.strip())
    if "*" in origins:
        raise CorsPolicyError(
            "CORS_ALLOWED_ORIGINS may not contain '*': these services answer with PHI. "
            "List the origins explicitly."
        )
    return origins


def is_allowed_origin(origin: str | None, allowed: Sequence[str]) -> bool:
    """Whether a request carrying ``origin`` may be served.

    ``origin`` is ``None`` when the request carries no ``Origin`` header, and
    that is **allowed**. A missing header is not a browser making a cross-origin
    request: it is a service-to-service caller, a test client, or a
    ``curl``. Refusing it would break every non-browser caller of the WebSocket
    endpoints — which is all of them today — while stopping nothing, since a
    non-browser client can send whatever ``Origin`` it likes anyway. CORS
    constrains browsers on behalf of their users; it is not an access control on
    the server, and treating it as one here would produce a check that blocks
    the honest callers and none of the dishonest ones.

    Matching is exact string equality against the configured list. An origin is
    a scheme, host and port, already normalised by the browser that sent it, so
    there is nothing to case-fold or trim — and a prefix or suffix match is how
    ``https://medauth.example.com.attacker.test`` comes to be treated as ours.
    """
    if origin is None:
        return True
    return origin in allowed


__all__ = [
    "ORIGIN_REFUSED_REASON",
    "CorsPolicyError",
    "is_allowed_origin",
    "parse_allowed_origins",
]
