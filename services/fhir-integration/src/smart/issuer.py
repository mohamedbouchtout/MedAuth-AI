"""Reading a SMART ``iss`` safely.

An ``iss`` is a FHIR base URL, and a FHIR base URL carries no query string or
fragment. Real launches occasionally arrive with one anyway — a proxy appending
a tracking parameter, a vendor's own launcher passing context along — and every
use this service makes of ``iss`` is wrong if it is taken at face value:

* ``discovery_url()`` appends a path to it, so a query string would produce
  ``https://host/r4?practice=x/.well-known/smart-configuration`` — a URL that
  addresses nothing and fails as a 404 the operator has to reverse-engineer.
* ``aud`` binds the authorization request to a FHIR server, so a value that
  differs from the server's own base URL by a stray parameter can be refused.
* it is stored as ``fhir_base_url`` and every later request is built on it.
* it reaches the HTTP client, whose own request logging then carries whatever
  the query string held — which is the reason CLAUDE.md says to log the host
  rather than the ``iss``, one layer further out than a log statement can reach.

So it is normalised once, at the edge, and the normalised value is the only one
that travels.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_fhir_base_url(iss: str) -> str:
    """Return the FHIR base URL an ``iss`` names, without query or fragment.

    Args:
        iss: The SMART launch ``iss`` parameter, as received.

    Returns:
        Scheme, host and path only, with any trailing slash removed so the
        discovery URL and the stored base URL agree on one spelling.
    """
    parts = urlsplit(iss.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def issuer_host(iss: str) -> str:
    """Return an issuer's host, for log lines and error messages.

    The host is the part that identifies the EHR; the rest can carry launch
    context, which does not belong in a log. Returns an empty string when there
    is no host to find, rather than raising — a malformed ``iss`` is a 502 with
    a message, not a crash inside the logging call that was meant to explain it.
    """
    return urlsplit(iss.strip()).hostname or ""
