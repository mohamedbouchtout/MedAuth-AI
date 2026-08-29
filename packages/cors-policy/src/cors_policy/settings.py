"""The settings field every service declares for its allowed origins.

Four services read ``CORS_ALLOWED_ORIGINS`` — two to install the middleware and
two to check a WebSocket ``Origin`` — so the field's type is defined once here
rather than annotated four times. Four copies of one annotation is the drift
this package exists to prevent, and one of them would eventually be the copy
that forgets ``NoDecode``.

``NoDecode`` is not optional and not stylistic. ``pydantic-settings`` tries to
JSON-decode any environment value destined for a complex type *before* field
validators run, so without it a plain ``a,b`` value raises

    SettingsError: error parsing value for field "cors_allowed_origins"
    from source "EnvSettingsSource"

and a ``BeforeValidator`` never sees the string at all. Verified against
pydantic-settings 2.15.0.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import NoDecode

from cors_policy.origins import parse_allowed_origins


def _parse(value: object) -> object:
    """Split a configured string; pass anything already structured through.

    A tuple arrives unchanged when a test constructs ``Settings`` directly
    rather than through the environment, which is how most of them do it.
    """
    if isinstance(value, str):
        return parse_allowed_origins(value)
    return value


#: The browser origins this service answers. Comma-separated in the
#: environment, empty by default — a service with no configured origins answers
#: no browser, which is the correct posture for one that has never had a browser
#: caller. ``*`` is refused by :func:`parse_allowed_origins` rather than being
#: quietly accepted; see CLAUDE.md, "CORS and browser reachability".
AllowedOrigins = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_parse)]


__all__ = ["AllowedOrigins"]
