"""Installing the one CORS policy on a service's FastAPI app.

The methods, headers and credentials flag are fixed here rather than passed in.
CLAUDE.md settles them as one decision for the repository — "the allowed methods
and headers, and whether credentials are permitted, are part of the same
decision rather than left to each route" — and a service that could pass its own
would be the per-service policy TASK-041c exists to refuse. Only the origins
vary, because only they are genuinely per environment.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

#: The methods a browser may use. Every verb this repository actually exposes to
#: one — ``POST`` for ``/sessions/*``, ``GET`` and ``PATCH`` for the note routes,
#: ``PATCH`` for the nudge acknowledge — plus ``OPTIONS`` for the preflight
#: itself. Deliberately not ``*`` and deliberately no ``DELETE``: the database
#: convention is soft deletes, so no route deletes anything and a browser has no
#: use for the verb.
ALLOWED_METHODS: Final = ("GET", "POST", "PATCH", "OPTIONS")

#: The request headers a browser may set. ``Content-Type`` is what makes a JSON
#: body preflight in the first place. ``Authorization`` is listed because the
#: session token's carrier is a header everywhere else in this repository, so a
#: browser-facing route that later takes one needs no change here — see the
#: credentials note below for why that is a header and never a cookie.
#:
#: ``X-MedAuth-Launch-Id`` carries the SMART ``launch_id`` to fhir-integration's
#: chart reads (TASK-052). It is a separate header rather than a second meaning
#: for ``Authorization``: that one carries the MedAuth session token everywhere
#: else here, and a ``launch_id`` is a different identifier with a different
#: lifetime — CLAUDE.md, "A SMART launch is not an encounter session". Growing
#: this tuple is how a browser-facing route gains a header; a service passing
#: its own list is the per-service policy TASK-041c refused.
ALLOWED_HEADERS: Final = ("Authorization", "Content-Type", "X-MedAuth-Launch-Id")

#: Whether the browser may send credentials — **no**, and this is load-bearing
#: rather than a default left alone.
#:
#: Nothing in this repository authenticates with a cookie: the session JWT
#: travels in an ``Authorization`` header or the ``medauth.jwt.`` subprotocol,
#: which are carriers a page must already hold a token to use. That is exactly
#: what makes the WebSocket endpoints safe from cross-site hijacking without an
#: ``Origin`` check (CLAUDE.md, "CORS and browser reachability"), so enabling
#: credentials here would quietly undermine reasoning written down elsewhere:
#: it invites a cookie, and a cookie is ambient. Leave this False until that
#: reasoning is deliberately revisited.
ALLOW_CREDENTIALS: Final = False


def install_cors(app: FastAPI, allowed_origins: Sequence[str]) -> None:
    """Install the repository's CORS policy on ``app``.

    Call it beside ``install_error_handlers(app)`` in a service's
    ``create_app()``. Installing nothing when ``allowed_origins`` is empty is
    deliberate: a service configured with no origins answers no browser, and
    adding middleware that rejects every origin would report the same outcome
    less clearly.
    """
    if not allowed_origins:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_methods=list(ALLOWED_METHODS),
        allow_headers=list(ALLOWED_HEADERS),
        allow_credentials=ALLOW_CREDENTIALS,
    )


__all__ = [
    "ALLOWED_HEADERS",
    "ALLOWED_METHODS",
    "ALLOW_CREDENTIALS",
    "install_cors",
]
