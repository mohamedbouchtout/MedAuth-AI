"""The repository's one CORS policy, imported by every service that needs it.

An HTTP service installs it beside the shared error handlers::

    from api_envelope import install_error_handlers
    from cors_policy import install_cors

    def create_app() -> FastAPI:
        app = FastAPI(...)
        install_error_handlers(app)
        install_cors(app, get_settings().cors_allowed_origins)
        return app

A WebSocket endpoint checks the origin for itself, because a browser applies no
CORS to an upgrade request::

    from cors_policy import ORIGIN_REFUSED_REASON, is_allowed_origin

    if not is_allowed_origin(websocket.headers.get("origin"), settings.cors_allowed_origins):
        logger.warning("Refused connection: %s", ORIGIN_REFUSED_REASON)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

**Scope note:** this package is the CORS policy and the origin list it is built
from. It is not a place for shared routes, authentication, dependencies or other
middleware — the same boundary ``api-envelope`` and ``session-auth`` draw, and
for the same reason. In particular it authenticates nobody: an allowed origin is
not an authorised caller, and CORS is a constraint a browser applies on behalf
of its user rather than an access control the server enforces.

It is a package rather than a few lines in each service because TASK-041c
settles the policy once for the repository. Two services install the middleware
(``track-a-clinical`` and ``track-b-rag``) and two more read the same origin
list for their WebSocket handshakes (``audio-ingestion`` and ``nudge-service``),
so a hand-written allow-list per service is four places for one policy to drift
— and a permissive middleware growing in one service is precisely how a
repo-wide policy gets set by accident. See CLAUDE.md, "CORS and browser
reachability", for why the policy lives in the services rather than in an
ingress, and for why that choice forecloses nothing about where authentication
eventually lands.

It exists as its own package rather than as an addition to ``api-envelope``
because that package's scope note is locked and excludes middleware; bolting
this on would make it the shared web framework it declares it is not.
"""

from cors_policy.middleware import (
    ALLOW_CREDENTIALS,
    ALLOWED_HEADERS,
    ALLOWED_METHODS,
    install_cors,
)
from cors_policy.origins import (
    ORIGIN_REFUSED_REASON,
    CorsPolicyError,
    is_allowed_origin,
    parse_allowed_origins,
)
from cors_policy.settings import AllowedOrigins

__all__ = [
    "ALLOWED_HEADERS",
    "AllowedOrigins",
    "ALLOWED_METHODS",
    "ALLOW_CREDENTIALS",
    "ORIGIN_REFUSED_REASON",
    "CorsPolicyError",
    "install_cors",
    "is_allowed_origin",
    "parse_allowed_origins",
]
