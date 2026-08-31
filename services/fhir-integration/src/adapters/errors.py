"""What can go wrong talking to an EHR's FHIR server, as four distinct failures.

**The distinction is the point of this module.** A single generic "FHIR call
failed" would let an outage read as a patient with no insurance: TASK-052b writes
`insurance_payer` from a `Coverage` fetch, and "the EHR holds no Coverage for this
patient" and "we could not reach the EHR" call for opposite handling. Collapsing
them is the same failure class this repository already refuses elsewhere — a
payer's silence read as a negative determination, `validation: null` read as
"checked and rejected".

The four, and what each tells a caller:

``FHIRResourceNotFound``
    The EHR answered and the resource does not exist. Stable; retrying changes
    nothing.
``FHIRUpstreamUnavailable``
    No usable answer — a connect error, a read timeout, or a 5xx. **The only one
    worth retrying.**
``FHIRMalformedResponse``
    A 200 carrying something that is not the resource asked for. Not transient:
    it means a vendor quirk needing an adapter subclass.
``FHIRAuthorizationExpired``
    The launch's access token is no longer good. Recognised in one place — see
    ``src/api/fhir.py`` — and it is the seam TASK-051b fills.

**No response body reaches any of these.** A FHIR error body carries patient
detail routinely, and ``OperationOutcome.diagnostics`` is written for humans
looking at a chart. Each exception names the resource type, the id asked for and
the transport-level outcome, which is what a caller and an operator need; the
body is discarded unread. Same rule TASK-051 follows for a token endpoint's
``error_description``.
"""

from __future__ import annotations


class FHIRAccessError(Exception):
    """Base for every failure reaching an EHR's FHIR server.

    Attributes:
        resource_type: The FHIR resource type being fetched, e.g. ``Patient``.
        resource_id: The id asked for. An identifier, never patient content.
    """

    def __init__(self, resource_type: str, resource_id: str, detail: str) -> None:
        """Record what was being fetched and how it failed.

        Args:
            resource_type: The FHIR resource type being fetched.
            resource_id: The id asked for.
            detail: A fixed description of the failure. Never derived from a
                response body — see the module docstring.
        """
        super().__init__(f"{resource_type}/{resource_id}: {detail}")
        self.resource_type = resource_type
        self.resource_id = resource_id


class FHIRResourceNotFound(FHIRAccessError):
    """The EHR answered, and the resource does not exist.

    An HTTP 404, or a 200 ``OperationOutcome`` whose issue code is
    ``not-found``. **An empty search Bundle is not this**: a
    ``Coverage?patient=`` returning no entries is a successful answer meaning
    the patient has none on file, and the coverage rule in TASK-052 handles it.
    """


class FHIRUpstreamUnavailable(FHIRAccessError):
    """No usable answer came back from the EHR.

    A connect error, a read timeout, or any 5xx. The one failure here worth
    retrying, which is why it is not merged with the other three.

    Attributes:
        timed_out: Whether the request timed out, which the route turns into a
            504 rather than a 502.
    """

    def __init__(
        self, resource_type: str, resource_id: str, detail: str, *, timed_out: bool = False
    ) -> None:
        """Record the failure and whether it was a timeout."""
        super().__init__(resource_type, resource_id, detail)
        self.timed_out = timed_out


class FHIRMalformedResponse(FHIRAccessError):
    """The EHR answered 200 with something that is not the resource asked for.

    Not JSON, the wrong ``resourceType``, or a body failing validation against
    the R4 model. Never retried: it is a vendor deviation, and the fix is an
    adapter subclass rather than a second attempt.
    """


class FHIRAuthorizationExpired(FHIRAccessError):
    """The EHR rejected the launch's access token — a 401 or 403.

    Raised by the primitives but **recognised in exactly one place**, the
    dependency that loads the launch token and builds the adapter. Until
    TASK-051b implements renewal this means the SMART launch must be repeated,
    which is the limitation TASK-051 already states. TASK-051b replaces that one
    handler and touches none of the fetches.
    """


__all__ = [
    "FHIRAccessError",
    "FHIRAuthorizationExpired",
    "FHIRMalformedResponse",
    "FHIRResourceNotFound",
    "FHIRUpstreamUnavailable",
]
