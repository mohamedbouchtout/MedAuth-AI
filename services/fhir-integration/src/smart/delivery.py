"""How a completed SMART launch is handed back to whoever started it.

`GET /fhir/callback` has two genuinely different callers. A service-to-service
caller reads the JSON answer TASK-051 has always returned. An application cannot:
the callback runs in whatever browser the EHR redirected, so JSON rendered there
reaches nobody, and the app that started the launch never learns its
``launch_id``.

**The JSON answer is supplemented, never replaced**, because that first caller is
already served correctly and breaking it to serve a second one trades one gap for
another. Which answer a launch gets is **declared by whoever initiated it** — the
``delivery`` parameter on ``GET /fhir/launch``, recorded on the
``fhir_launch:{state}`` record, reaching the callback on the callback's own
request through ``state``. Nothing infers it from ``Accept``, ``User-Agent`` or
any other part of the request, and neither shape is a silent default: a caller
that cannot see how its response shape was chosen cannot tell a wrong guess from
a correct answer.

See CLAUDE.md, "Handing a completed SMART launch back to a client", which settles
all of this once because TASK-025c and TASK-070 both consume it.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit


class LaunchDelivery(StrEnum):
    """How the completed launch reaches whoever started it.

    A closed vocabulary, and a ``StrEnum`` for the same reason ``EHRType`` is
    one: the value **round-trips through Redis** — written onto the pending
    launch at the redirect and read back at the callback — so a free-form string
    would put the write side and the read side in two modules with nothing
    holding them in step. A ``StrEnum`` member compares equal to its own text, so
    what goes into Redis and comes back out is an ordinary string and no
    serialisation step has to know about the type.
    """

    #: Answer with the ``{launch_id, ehr_type, expires_in}`` envelope, in the
    #: response body. What TASK-051 has always done, and what a
    #: service-to-service caller reads.
    JSON = "json"

    #: Redirect the browser to ``SMART_WEB_RETURN_URL`` with a claim code.
    WEB = "web"

    #: Redirect to ``SMART_MOBILE_RETURN_URI`` — a custom scheme the OS routes
    #: back to the app that opened the auth session — with a claim code.
    MOBILE = "mobile"


#: The query parameter the claim code rides back on. It is not the ``launch_id``:
#: that is a capability handle and never goes in a URL. See ``store.LaunchClaim``
#: for what makes a code safe to carry here.
CLAIM_QUERY_PARAM = "claim"


def redirect_target_for(
    delivery: LaunchDelivery,
    *,
    web_return_url: str,
    mobile_return_uri: str,
) -> str | None:
    """Return the configured return target for one delivery, or ``None`` for JSON.

    Args:
        delivery: What the launch's initiator declared.
        web_return_url: ``SMART_WEB_RETURN_URL``, already validated at startup.
        mobile_return_uri: ``SMART_MOBILE_RETURN_URI``, likewise.

    Returns:
        The target to redirect to, or ``None`` when this delivery answers in the
        response body instead.
    """
    match delivery:
        case LaunchDelivery.WEB:
            return web_return_url
        case LaunchDelivery.MOBILE:
            return mobile_return_uri
        case LaunchDelivery.JSON:
            return None


def append_claim(target: str, claim: str) -> str:
    """Return ``target`` carrying ``claim`` as its query string.

    Plain concatenation rather than ``urlencode``: a target is validated at
    startup to carry no query and no fragment, and a claim code is base64url —
    so neither side has anything to escape. Doing it this way keeps a custom
    scheme URI like ``medauth://launch`` intact, which ``urlunsplit`` does not
    reliably round-trip for schemes it does not recognise as hierarchical.

    Args:
        target: A validated return target.
        claim: The single-use handoff code.

    Returns:
        The URL to redirect the browser to.
    """
    return f"{target}?{CLAIM_QUERY_PARAM}={claim}"


class ReturnTargetError(ValueError):
    """A configured return target is missing or malformed.

    Raised at startup, never during a launch. The whole point of validating
    these at boot is that the alternative surfaces at the *end* of an OAuth
    redirect chain — after discovery, after a human has logged in, after a token
    exchange has spent a real credential — which is the worst place in this
    system to discover a configuration error, because there is nothing left to
    do but start over and nothing in the browser saying why.
    """


def _reject_query_or_fragment(variable: str, value: str, parts: object) -> None:
    """Refuse a target that already carries a query string or a fragment."""
    query = getattr(parts, "query", "")
    fragment = getattr(parts, "fragment", "")
    if query or fragment:
        raise ReturnTargetError(
            f"{variable} must carry no query string and no fragment — the launch "
            f"claim is appended as one, and a target that already has a query "
            f"would silently produce two. Got: {value!r}"
        )


def validate_web_return_url(value: str) -> str:
    """Validate ``SMART_WEB_RETURN_URL`` and return it unchanged.

    An absolute ``https://`` URL. ``http://`` is accepted **only** for
    ``localhost`` and ``127.0.0.1``, which is what local development and CI
    actually run — ``apps/web`` is served by Vite over plaintext and the compose
    stack has no TLS. Everywhere else "TLS everywhere" applies, and a plaintext
    return target would carry a launch claim in the clear.

    Raises:
        ReturnTargetError: When the value is empty, relative, or not one of the
            two accepted shapes.
    """
    variable = "SMART_WEB_RETURN_URL"
    if not value:
        raise ReturnTargetError(
            f"{variable} is not set. It is where a browser is sent once a "
            f"delivery=web launch completes, and a launch cannot be handed to "
            f"apps/web without it."
        )

    parts = urlsplit(value)
    if not parts.hostname:
        raise ReturnTargetError(f"{variable} must be an absolute URL with a host. Got: {value!r}")

    is_loopback = parts.hostname in ("localhost", "127.0.0.1")
    if parts.scheme == "https" or (parts.scheme == "http" and is_loopback):
        _reject_query_or_fragment(variable, value, parts)
        return value

    raise ReturnTargetError(
        f"{variable} must be an https:// URL — http:// is accepted only for "
        f"localhost and 127.0.0.1, which is what local development runs. "
        f"Got: {value!r}"
    )


def validate_mobile_return_uri(value: str) -> str:
    """Validate ``SMART_MOBILE_RETURN_URI`` and return it unchanged.

    A custom scheme URI such as ``medauth://launch``. ``http`` and ``https`` are
    refused here rather than merely discouraged: the point of this value is a
    scheme the OS routes back to the app that opened the auth session, and an
    https URL in this setting would open a web page and strand the launch in a
    browser the app cannot read.

    Raises:
        ReturnTargetError: When the value is empty, carries no scheme, or uses a
            web scheme.
    """
    variable = "SMART_MOBILE_RETURN_URI"
    if not value:
        raise ReturnTargetError(
            f"{variable} is not set. It is where the system browser is sent once "
            f"a delivery=mobile launch completes, and a launch cannot be handed "
            f"back to apps/mobile without it."
        )

    parts = urlsplit(value)
    if not parts.scheme:
        raise ReturnTargetError(
            f"{variable} must be an absolute URI with a custom scheme, such as "
            f"medauth://launch. Got: {value!r}"
        )
    if parts.scheme in ("http", "https"):
        raise ReturnTargetError(
            f"{variable} must use a custom scheme the OS routes back to the app, "
            f"not {parts.scheme}:// — a web URL here opens a browser page the "
            f"app cannot read. Got: {value!r}"
        )

    _reject_query_or_fragment(variable, value, parts)
    return value
