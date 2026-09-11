"""What each third-party library is allowed to write, and why.

Every entry in :data:`LIBRARY_LOG_FLOORS` is a floor that was chosen by running
the library and reading what it emitted, not by reputation. The level is set
*just above where that library writes request or response content* rather than
uniformly to ``WARNING``, so each library keeps whatever it says that is useful
and loses only the part that carries clinical data. That is why the table is a
mapping with reasons rather than one call in a loop over a list of names.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

#: Minimum level per third-party logger, keyed by the logger name the library
#: uses. Read as: records below this level never leave the process.
#:
#: ``httpx`` — **WARNING, and it is the reason this package exists.** It logs
#: every request it makes at INFO as ``HTTP Request: GET <full url> "HTTP/1.1
#: 200 OK"``, and that is the *whole* URL, not only its query string. Both
#: halves carry patient data in this repository: ``_search`` issues
#: ``Coverage?patient={id}`` and ``Condition?patient={id}``, TASK-025b's search
#: issues ``Patient?name=<a patient's name>``, and ``get_patient()`` reads
#: ``Patient/{patient_id}`` — a patient identifier in the path, on the most
#: ordinary read the adapter makes. The floor is WARNING because the leak is at
#: INFO; nothing lower would close it.
#:
#: ``httpcore`` — INFO. Verified *not* to carry the URL: its trace lines render
#: the request as ``<Request [b'GET']>``, which is the method and nothing else,
#: and it logs no request headers. So this entry is not fixing a leak. It is
#: here because httpcore is the transport underneath httpx and its DEBUG trace
#: is the same class of output — a future version that starts rendering more of
#: the request would otherwise arrive silently, in the one library guaranteed to
#: see every URL httpx sends.
#:
#: ``urllib3`` — INFO. ``urllib3.connectionpool`` writes the full request line
#: at DEBUG, query string included: ``http://host "GET /Patient?name=... 200``.
#: It is botocore's transport, so this is reachable from any AWS call as well as
#: from any library in the tree that still uses ``requests``.
#:
#: ``botocore`` — INFO, and this is the largest body of PHI of the five.
#: ``botocore.endpoint`` logs the entire outgoing request at DEBUG, headers and
#: body together, and ``botocore.parsers`` logs the entire response body at
#: DEBUG. A Bedrock request body is an encounter's accumulated transcript and
#: its response body is the generated SOAP note; a Comprehend Medical request
#: body is clinical text. Note what the floor protects and what it does not:
#: INFO keeps ``botocore.credentials`` reporting where credentials were found,
#: which is operationally useful and carries nothing.
#:
#: ``boto3`` — INFO. Its own logger is nearly silent, and it is floored
#: alongside ``botocore`` so the pair cannot drift apart: a reader turning "boto
#: logging" up would otherwise get a different answer depending on which of the
#: two names they reached for.
LIBRARY_LOG_FLOORS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "httpx": logging.WARNING,
        "httpcore": logging.INFO,
        "urllib3": logging.INFO,
        "botocore": logging.INFO,
        "boto3": logging.INFO,
    }
)


def install_logging_policy() -> None:
    """Raise every library logger in :data:`LIBRARY_LOG_FLOORS` to its floor.

    Call it first in a service's ``create_app()``, or at the top of a one-shot
    job's ``main()``. Idempotent, so repeated calls — one per app a test suite
    builds — are free and have no cumulative effect.

    **It raises and never lowers.** A logger already pinned above its floor is
    left alone: someone silencing a library further has made a deliberate choice
    in the same direction as this policy, and overriding it would be this
    package loosening a restriction rather than applying one. A logger set
    *below* its floor is raised, which is the whole point — the default is that
    a level nobody chose lets request content through.

    **What it cannot do is win against a later explicit change.** A call to
    ``logging.getLogger("httpx").setLevel(logging.DEBUG)`` after this runs takes
    effect, because that is how the stdlib works and because a developer doing
    it deliberately is not the failure mode here. The failure mode is a library
    writing PHI at a level nobody configured at all, and a level set on the
    library's own logger closes that regardless of what the root logger is set
    to afterwards.
    """
    for name, floor in LIBRARY_LOG_FLOORS.items():
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < floor:
            logger.setLevel(floor)


__all__ = ["LIBRARY_LOG_FLOORS", "install_logging_policy"]
