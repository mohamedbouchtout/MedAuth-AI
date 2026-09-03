"""``EHRAdapter`` — standard FHIR R4 / US Core, working on every EHR.

All EHR integration goes through this layer. Route handlers never import a
concrete adapter; they call ``get_adapter()`` and use whatever comes back. See
CLAUDE.md, "Adapter Architecture", which this module implements.

**The methods are in two layers, and the distinction decides where a vendor's
deviation goes.** Primitives read one resource type each and compose nothing.
``get_patient_context()`` is composed: it assembles three primitives, and it is
the method Cerner and Epic override (TASK-056, TASK-057), so those subclasses
can call ``super()`` and adjust the assembled result instead of reimplementing
three fetches.

TASK-052 implements the fetches. TASK-052b adds the ``Location`` and
``Organization`` primitives and ``get_encounter_coverage_context()``, which is
the second composed method and the one that answers "which payer policy set,
and where". TASK-053 implements the note write-back, which is the first *write* to an EHR
in this repository: see ``_create`` below, and ``note_document.py`` for what is
composed and which codes are allowed to leave the system. TASK-054 still owns the
prior authorization submission, which remains a stub.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

import httpx
from pydantic import ValidationError

from fhir_types import Bundle, Condition, Coverage, Encounter, Location, Organization, Patient

from .errors import (
    FHIRAuthorizationExpired,
    FHIRMalformedResponse,
    FHIRResourceNotFound,
    FHIRUpstreamUnavailable,
)
from .models import (
    ClinicalNoteContent,
    CoverageInfo,
    EncounterCoverageContext,
    PatientContext,
    PatientInfo,
    PriorAuthContent,
    PriorAuthSubmission,
    SubmissionMethod,
)
from .note_document import build_document_reference
from .pas_bundle import (
    build_request_bundle,
    payer_reference_number,
    read_response_bundle,
    submission_outcome,
    unknown_entry_types,
)
from .site_of_care import (
    location_state,
    log_state_disagreement,
    organization_state,
    patient_address_state,
    reference_id,
    service_provider_reference,
    site_location_references,
    to_usps_state,
)

logger = logging.getLogger(__name__)

#: Per-call timeout, set here rather than on the shared client, which is also
#: used for unauthenticated discovery against other hosts. Matches the per-call
#: arrangement ``smart/discovery.py`` and ``smart/oauth.py`` already use.
FHIR_TIMEOUT_SECONDS: Final = 10.0

#: US Core says an active condition is one whose ``clinicalStatus`` is any of
#: these. ``active`` alone would drop a relapsed or recurring problem, which a
#: payer's criteria may well turn on.
_ACTIVE_CLINICAL_STATUSES: Final = frozenset({"active", "recurrence", "relapse"})


class EHRAdapter:
    """Standard FHIR R4 / US Core access to one EHR, needing no vendor knowledge.

    **Concrete and instantiable on purpose.** This is not only the base other
    adapters extend — it is the adapter an unrecognised issuer is routed to, per
    the fallback in ``factory.detect_ehr_from_issuer()``. Making it abstract
    would turn every SMART launch from an EHR we have not seen before into a
    failed launch, which is the hard failure that fallback exists to avoid. An
    EHR we do not recognise is usually still a conformant FHIR R4 server.

    The access token is a credential. It is held privately and kept out of
    ``__repr__`` deliberately: an adapter reaches error paths and log lines that
    a token must not, and the cheapest way to guarantee that is for the object
    never to render it. Subclasses must not expose it either.

    **The HTTP client is injected, never constructed here.** It is the
    process-wide ``httpx.AsyncClient`` from ``src/api/dependencies.py``, so
    connections to a vendor's FHIR server pool across requests exactly as they
    already do for discovery and token exchange, and a test substitutes a
    transport without patching anything. An adapter that built its own would
    open a pool per request and put the one object that must not render its
    access token in charge of its own transport.
    """

    def __init__(
        self, fhir_base_url: str, access_token: str, http_client: httpx.AsyncClient
    ) -> None:
        """Bind the adapter to one EHR's FHIR endpoint and one launch's token.

        Args:
            fhir_base_url: Base URL of the EHR's FHIR R4 server.
            access_token: The SMART on FHIR access token for this launch.
            http_client: The shared HTTP client. See the class docstring.
        """
        self.fhir_base_url = fhir_base_url.rstrip("/")
        self._access_token = access_token
        self._http = http_client

    def __repr__(self) -> str:
        """Render the adapter without its access token. See the class docstring."""
        return f"{type(self).__name__}(fhir_base_url={self.fhir_base_url!r})"

    # -- The one place an HTTP call to an EHR is made -------------------------

    async def _get(
        self,
        path: str,
        *,
        resource_type: str,
        resource_id: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET one FHIR path and return its decoded body.

        Every failure mode is turned into one of the four in ``errors.py`` here,
        so no caller has to interpret an HTTP status and none of them can
        disagree about what a 404 or a 502 means.

        **The ``Authorization`` header is applied per request**, never as a
        default on the shared client, which also talks to hosts that must not
        see this token.

        Args:
            path: Path below the FHIR base URL, e.g. ``Patient/123``.
            resource_type: What is being fetched, for the error message.
            resource_id: The id asked for, for the error message.
            params: Search parameters, when this is a search rather than a read.

        Returns:
            The decoded JSON body.

        Raises:
            FHIRAuthorizationExpired: The EHR rejected the token (401/403).
            FHIRResourceNotFound: The EHR answered 404.
            FHIRUpstreamUnavailable: Transport failure or a 5xx.
            FHIRMalformedResponse: A 200 whose body is not JSON.
        """
        try:
            response = await self._http.get(
                f"{self.fhir_base_url}/{path}",
                params=params,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/fhir+json",
                },
                timeout=FHIR_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise FHIRUpstreamUnavailable(
                resource_type, resource_id, "the EHR did not answer in time", timed_out=True
            ) from exc
        except httpx.HTTPError as exc:
            # Deliberately not ``str(exc)``: httpx puts the full request URL in
            # its message, and a search URL carries a patient id.
            raise FHIRUpstreamUnavailable(
                resource_type, resource_id, "the EHR could not be reached"
            ) from exc

        if response.status_code in (401, 403):
            raise FHIRAuthorizationExpired(
                resource_type, resource_id, "the EHR rejected this launch's access token"
            )
        if response.status_code == 404:
            raise FHIRResourceNotFound(resource_type, resource_id, "no such resource on the EHR")
        if response.status_code >= 500:
            raise FHIRUpstreamUnavailable(
                resource_type, resource_id, f"the EHR answered {response.status_code}"
            )
        if response.status_code >= 400:
            # A 4xx that is not one of the above is the EHR refusing the request
            # as we built it — a malformed search, an unsupported parameter.
            # That is our bug rather than an outage, and not worth retrying.
            raise FHIRMalformedResponse(
                resource_type, resource_id, f"the EHR refused the request ({response.status_code})"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise FHIRMalformedResponse(
                resource_type, resource_id, "the EHR's response was not JSON"
            ) from exc

        if not isinstance(body, dict):
            raise FHIRMalformedResponse(
                resource_type, resource_id, "the EHR's response was not a FHIR resource"
            )

        self._raise_if_not_found_outcome(body, resource_type, resource_id)
        return body

    # -- The one place a resource is created on an EHR ------------------------

    async def _create(
        self, resource_type: str, resource: dict[str, Any], *, resource_id: str
    ) -> str:
        """POST one FHIR resource and return the id the EHR assigned it.

        The counterpart to :meth:`_get`, and the second HTTP call site in this
        class rather than a general-purpose one — a write earns its own method
        because a create has answers a read does not: the id comes back in a
        header, and "accepted but we cannot tell you what was created" is a real
        outcome that must not be reported as success.

        The four failures in ``errors.py`` mean the same things they do on a
        read, with one addition worth stating: a 4xx that is not 401/403/404 is
        **malformed**, not unavailable. The EHR refusing the resource we built is
        our bug or a vendor quirk needing a subclass, and retrying an identical
        request would only ask the same question again — or, worse, land a second
        document if the first was in fact accepted.

        Args:
            resource_type: The type being created, e.g. ``DocumentReference``.
            resource: The resource body, already dumped with FHIR's own element
                names.
            resource_id: What to name in an error. **Never the created id** —
                there is none yet — so callers pass the thing the write was
                about, such as the encounter.

        Returns:
            The id the EHR assigned the new resource.

        Raises:
            FHIRAuthorizationExpired: The EHR rejected the token (401/403).
            FHIRResourceNotFound: The EHR answered 404 — the endpoint does not
                accept this resource type.
            FHIRUpstreamUnavailable: Transport failure or a 5xx.
            FHIRMalformedResponse: The resource was refused, or accepted without
                the EHR saying what it created.
        """
        try:
            response = await self._http.post(
                f"{self.fhir_base_url}/{resource_type}",
                json=resource,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/fhir+json",
                    "Content-Type": "application/fhir+json",
                },
                timeout=FHIR_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            # A timeout on a create is genuinely ambiguous — the EHR may have
            # filed the document anyway. It is reported as the transient failure
            # it is, and the caller does not retry automatically: the route
            # leaves the note unrecorded so a person decides, rather than risking
            # a second document on a chart.
            raise FHIRUpstreamUnavailable(
                resource_type, resource_id, "the EHR did not answer in time", timed_out=True
            ) from exc
        except httpx.HTTPError as exc:
            raise FHIRUpstreamUnavailable(
                resource_type, resource_id, "the EHR could not be reached"
            ) from exc

        if response.status_code in (401, 403):
            raise FHIRAuthorizationExpired(
                resource_type, resource_id, "the EHR rejected this launch's access token"
            )
        if response.status_code == 404:
            raise FHIRResourceNotFound(
                resource_type, resource_id, "the EHR does not accept this resource type"
            )
        if response.status_code >= 500:
            raise FHIRUpstreamUnavailable(
                resource_type, resource_id, f"the EHR answered {response.status_code}"
            )
        if response.status_code >= 400:
            raise FHIRMalformedResponse(
                resource_type, resource_id, f"the EHR refused the resource ({response.status_code})"
            )

        created_id = self._created_id(response, resource_type)
        if created_id is None:
            raise FHIRMalformedResponse(
                resource_type,
                resource_id,
                "the EHR accepted the resource without saying what it created",
            )
        return created_id

    @staticmethod
    def _created_id(response: httpx.Response, resource_type: str) -> str | None:
        """Read the new resource's id out of a create response.

        Two shapes are accepted because both are conformant and vendors differ.
        The ``Location`` header is the normal answer to a create and is checked
        first — R4 gives it as ``[base]/[type]/[id]`` optionally followed by
        ``/_history/[vid]``, so the id is the segment after the type. A server
        configured to echo the resource answers with a body carrying ``id``
        instead, which is checked second.

        Neither is trusted to be well-formed: a header naming a different
        resource type, or a body that is not JSON, yields ``None`` and the caller
        reports a malformed response rather than inventing an id. An id we made
        up would be written to ``clinical_notes`` as though it named a real
        document.
        """
        location: str | None = response.headers.get("location") or response.headers.get(
            "content-location"
        )
        if location:
            segments = [segment for segment in location.split("/") if segment]
            if resource_type in segments:
                index = segments.index(resource_type)
                if index + 1 < len(segments):
                    return segments[index + 1]

        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict) and body.get("resourceType") == resource_type:
            created = body.get("id")
            if isinstance(created, str) and created:
                return created
        return None

    @staticmethod
    def _raise_if_not_found_outcome(
        body: dict[str, Any], resource_type: str, resource_id: str
    ) -> None:
        """Treat a 200 ``OperationOutcome`` saying ``not-found`` as a not-found.

        Some servers answer a missing resource with 200 and an outcome rather
        than a 404. Only the issue *code* is read — never ``diagnostics``, which
        is free text written for someone looking at a chart and routinely
        carries patient detail.
        """
        if body.get("resourceType") != "OperationOutcome":
            return
        issues = body.get("issue")
        codes = (
            {issue.get("code") for issue in issues if isinstance(issue, dict)}
            if isinstance(issues, list)
            else set()
        )
        if "not-found" in codes:
            raise FHIRResourceNotFound(resource_type, resource_id, "no such resource on the EHR")
        raise FHIRMalformedResponse(
            resource_type,
            resource_id,
            "the EHR returned an OperationOutcome instead of the resource",
        )

    @staticmethod
    def _parse(model: type[Any], body: dict[str, Any], resource_type: str, resource_id: str) -> Any:
        """Validate a decoded body against its R4 model.

        Raises:
            FHIRMalformedResponse: Wrong ``resourceType``, or a body the model
                rejects. The validation error itself is not carried into the
                message — pydantic echoes offending values, and those are PHI.
        """
        if body.get("resourceType") != resource_type:
            raise FHIRMalformedResponse(
                resource_type,
                resource_id,
                f"the EHR returned a {body.get('resourceType')!r} resource",
            )
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise FHIRMalformedResponse(
                resource_type, resource_id, "the EHR's resource did not validate against FHIR R4"
            ) from exc

    async def _search(
        self, resource_type: str, patient_id: str, extra: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Run a ``?patient=`` search and return the entry resources.

        **An empty Bundle is a successful answer, not a not-found.** A patient
        with no ``Coverage`` on file is an ordinary case that the coverage rule
        handles; turning it into a 404 would make "no insurance recorded" and
        "no such patient" the same outcome.
        """
        params = {"patient": patient_id}
        if extra:
            params.update(extra)
        body = await self._get(
            resource_type, resource_type=resource_type, resource_id=patient_id, params=params
        )
        if body.get("resourceType") != "Bundle":
            raise FHIRMalformedResponse(
                resource_type, patient_id, "the EHR did not return a search Bundle"
            )
        entries = body.get("entry") or []
        if not isinstance(entries, list):
            raise FHIRMalformedResponse(
                resource_type, patient_id, "the search Bundle's entries were not a list"
            )
        return [
            entry["resource"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
        ]

    # -- The one place a FHIR operation is invoked ----------------------------

    async def _invoke(
        self, operation_path: str, resource: dict[str, Any], *, resource_id: str
    ) -> dict[str, Any]:
        """POST to a FHIR operation and return the resource it answered with.

        The third HTTP call site in this class, and its own method for the same
        reason :meth:`_create` is one rather than a general ``_request``: an
        operation answers differently from both a read and a create. It is a POST
        like a create, but there is no ``Location`` header and no created id —
        the answer *is* the body, and for ``Claim/$submit`` that body is a whole
        response bundle.

        The four failures in ``errors.py`` mean the same things they do
        elsewhere, with the same reasoning about which is which: a 4xx that is
        not 401/403/404 is **malformed** rather than unavailable, because the
        payer refusing what we built is our bug or a vendor quirk and asking the
        same question again would only be refused again.

        **A server that does not implement the operation answers 400, not 404** —
        checked against a real HAPI server, which returns an ``OperationOutcome``
        with issue code ``not-supported``. So an endpoint that does not speak PAS
        arrives here as malformed rather than as not-found, and that reading is
        right: the fix is to submit through another path for that payer, never to
        retry. See ``tests/integration/test_hapi_fhir.py``, which asserts it
        against the real server rather than against an assumption.

        **A timeout here is genuinely ambiguous and is never retried
        automatically.** The payer may have accepted the submission. The route
        leaves it unrecorded so a person decides, rather than risking a second
        prior authorization — same rule, and same reasoning, as a create.

        Args:
            operation_path: The operation below the FHIR base URL, e.g.
                ``Claim/$submit``.
            resource: The parameter resource, already dumped with FHIR's own
                element names.
            resource_id: What to name in an error. Never anything from the
                payload — for a submission it is the request's own id.

        Returns:
            The decoded response body.

        Raises:
            FHIRAuthorizationExpired: The endpoint rejected the token (401/403).
            FHIRResourceNotFound: The endpoint answered 404 — it does not
                implement this operation.
            FHIRUpstreamUnavailable: Transport failure or a 5xx.
            FHIRMalformedResponse: The operation was refused, or answered with
                something that is not a FHIR resource.
        """
        try:
            response = await self._http.post(
                f"{self.fhir_base_url}/{operation_path}",
                json=resource,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/fhir+json",
                    "Content-Type": "application/fhir+json",
                },
                timeout=FHIR_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise FHIRUpstreamUnavailable(
                operation_path, resource_id, "the payer did not answer in time", timed_out=True
            ) from exc
        except httpx.HTTPError as exc:
            # Deliberately not ``str(exc)``: httpx puts the request URL in its
            # message, and this one is a payer's endpoint plus our operation.
            raise FHIRUpstreamUnavailable(
                operation_path, resource_id, "the payer's endpoint could not be reached"
            ) from exc

        if response.status_code in (401, 403):
            raise FHIRAuthorizationExpired(
                operation_path, resource_id, "the endpoint rejected this launch's access token"
            )
        if response.status_code == 404:
            raise FHIRResourceNotFound(
                operation_path, resource_id, "the endpoint does not implement this operation"
            )
        if response.status_code >= 500:
            raise FHIRUpstreamUnavailable(
                operation_path, resource_id, f"the endpoint answered {response.status_code}"
            )
        if response.status_code >= 400:
            raise FHIRMalformedResponse(
                operation_path,
                resource_id,
                f"the endpoint refused the request ({response.status_code})",
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise FHIRMalformedResponse(
                operation_path, resource_id, "the endpoint's response was not JSON"
            ) from exc

        if not isinstance(body, dict):
            raise FHIRMalformedResponse(
                operation_path, resource_id, "the endpoint's response was not a FHIR resource"
            )
        return body

    # -- Primitives: one resource type each, no composition -------------------

    async def get_patient(self, patient_id: str) -> PatientInfo:
        """Primitive. Read the patient's demographics.

        Args:
            patient_id: The patient's id on this EHR.

        Returns:
            The patient's demographics, flattened out of ``Patient``.
        """
        body = await self._get(
            f"Patient/{patient_id}", resource_type="Patient", resource_id=patient_id
        )
        patient: Patient = self._parse(Patient, body, "Patient", patient_id)

        name = next(iter(patient.name or []), None)
        return PatientInfo(
            patient_id=patient.id or patient_id,
            family_name=name.family if name else None,
            given_names=list(name.given or []) if name else [],
            birth_date=patient.birth_date,
            gender=patient.gender,
            # For the site-of-care disagreement check only, never as a source
            # for the encounter's state. See ``site_of_care``.
            address_state=to_usps_state(patient_address_state(patient.address)),
        )

    async def get_coverage(self, patient_id: str) -> CoverageInfo | None:
        """Primitive. Read the patient's insurance coverage.

        Returns None when the EHR holds no usable ``Coverage`` — none at all,
        none active, or several active with no unambiguous primary. Partial
        coverage is not an error and must not be filled in with a guess; see
        ``PatientContext.requires_manual_confirmation`` and the enumerated table
        in TASK-052.

        **Selecting among several active coverages goes by ``Coverage.order``**,
        FHIR's own coordination-of-benefits ranking, and a tie is not broken.
        Picking one arbitrarily would answer a policy query against a secondary
        payer — a confident wrong answer, where an empty answer plus a visible
        signal is this repository's standing preference.

        Args:
            patient_id: The patient's id on this EHR.

        Returns:
            The payer, plan type and member id, or None.
        """
        resources = await self._search("Coverage", patient_id)
        coverages = [
            self._parse(Coverage, resource, "Coverage", patient_id)
            for resource in resources
            if resource.get("resourceType") == "Coverage"
        ]
        active = [coverage for coverage in coverages if coverage.status == "active"]
        if not active:
            return None

        chosen = self._primary_coverage(active)
        if chosen is None:
            logger.warning(
                "Patient has %d active Coverage resources with no unambiguous primary "
                "(Coverage.order absent or tied) — returning no coverage and asking for "
                "manual confirmation rather than guessing a payer.",
                len(active),
            )
            return None

        return CoverageInfo(
            payer=self._payer_display(chosen),
            plan_type=self._plan_type(chosen),
            member_id=self._member_id(chosen),
        )

    @staticmethod
    def _primary_coverage(active: list[Coverage]) -> Coverage | None:
        """Return the unambiguous primary coverage, or None when there is none."""
        if len(active) == 1:
            return active[0]

        ordered = [coverage for coverage in active if coverage.order is not None]
        if not ordered:
            return None
        lowest = min(coverage.order for coverage in ordered if coverage.order is not None)
        candidates = [coverage for coverage in ordered if coverage.order == lowest]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _payer_display(coverage: Coverage) -> str | None:
        """Return the payer's own spelling from ``Coverage.payor``.

        **Never slugged here.** ``payer_vocab.normalize_payer()`` is called by
        ``/policies/query`` and that stays the single normalisation site; a slug
        stored in a column documented as the payer's own spelling is the drift
        this repository already fixed once.
        """
        for payor in coverage.payor:
            if payor.display:
                return payor.display
            if payor.identifier is not None and payor.identifier.value:
                return payor.identifier.value
        return None

    @staticmethod
    def _plan_type(coverage: Coverage) -> str | None:
        """Return the plan type from ``Coverage.type``, falling back to ``class``.

        US Core puts the plan category in ``type``; several EHRs carry it only
        as the ``class`` entry whose type coding is ``plan``. Both are standard,
        so both are read here rather than in a vendor subclass.
        """
        if coverage.type is not None:
            if coverage.type.text:
                return coverage.type.text
            for coding in coverage.type.coding or []:
                if coding.code:
                    return coding.code

        for entry in coverage.coverage_class or []:
            codes = {coding.code for coding in entry.type.coding or []}
            if "plan" in codes:
                return entry.name or entry.value
        return None

    @staticmethod
    def _member_id(coverage: Coverage) -> str | None:
        """Return the member id from ``subscriberId``, falling back to an identifier."""
        if coverage.subscriber_id:
            return coverage.subscriber_id
        for identifier in coverage.identifier or []:
            if identifier.value:
                return identifier.value
        return None

    async def get_conditions(self, patient_id: str) -> list[Condition]:
        """Primitive. Read the patient's active conditions.

        "Active" follows US Core rather than a literal ``active`` match:
        ``recurrence`` and ``relapse`` are active problems too, and a payer's
        criteria may turn on exactly those.

        Args:
            patient_id: The patient's id on this EHR.

        Returns:
            The active ``Condition`` resources, empty when there are none.
        """
        resources = await self._search("Condition", patient_id)
        conditions = [
            self._parse(Condition, resource, "Condition", patient_id)
            for resource in resources
            if resource.get("resourceType") == "Condition"
        ]
        return [condition for condition in conditions if self._is_active(condition)]

    @staticmethod
    def _is_active(condition: Condition) -> bool:
        """Return whether a condition's ``clinicalStatus`` counts as active."""
        status = condition.clinical_status
        if status is None:
            # A condition with no clinical status is not assertably resolved, and
            # dropping it would hide a problem a payer's criteria may need.
            return True
        codes = {coding.code for coding in status.coding or []}
        return bool(codes & _ACTIVE_CLINICAL_STATUSES)

    async def get_encounter(self, encounter_id: str) -> Encounter:
        """Primitive. Read one encounter.

        Args:
            encounter_id: The encounter's id on this EHR.

        Returns:
            The ``Encounter`` resource.
        """
        body = await self._get(
            f"Encounter/{encounter_id}", resource_type="Encounter", resource_id=encounter_id
        )
        encounter: Encounter = self._parse(Encounter, body, "Encounter", encounter_id)
        return encounter

    async def get_location(self, location_id: str) -> Location:
        """Primitive. Read one location.

        Added by TASK-052b. It is a primitive rather than something folded into
        ``get_encounter()`` because it reads a different resource type: the
        two-layer rule in this module's docstring is what keeps a vendor's
        deviation landing in the right place, and a fetch that quietly read two
        resource types would have no honest layer to belong to.

        Args:
            location_id: The location's id on this EHR.

        Returns:
            The ``Location`` resource.
        """
        body = await self._get(
            f"Location/{location_id}", resource_type="Location", resource_id=location_id
        )
        location: Location = self._parse(Location, body, "Location", location_id)
        return location

    async def get_organization(self, organization_id: str) -> Organization:
        """Primitive. Read one organization.

        Args:
            organization_id: The organization's id on this EHR.

        Returns:
            The ``Organization`` resource.
        """
        body = await self._get(
            f"Organization/{organization_id}",
            resource_type="Organization",
            resource_id=organization_id,
        )
        organization: Organization = self._parse(
            Organization, body, "Organization", organization_id
        )
        return organization

    # -- Composed: assembles primitives, and is the override point ------------

    async def get_patient_context(self, patient_id: str) -> PatientContext:
        """Composed. Assemble ``get_patient()``, ``get_coverage()`` and ``get_conditions()``.

        **This is the method a vendor subclass overrides**, because enrichment
        and fallback are about the assembled context rather than one fetch: Epic
        adds proprietary extensions to it, Cerner fills a payer field the
        ``Coverage`` fetch returned incomplete. An override calls ``super()``
        and adjusts what comes back — it does not reimplement the three fetches,
        which is what overriding a primitive would force.

        The three run concurrently: they are independent reads against one
        server, and a context assembled serially pays three round trips of
        latency for no benefit. A failure in any of them propagates, which is
        why ``asyncio.gather`` is used without ``return_exceptions``.

        Args:
            patient_id: The patient's id on this EHR.

        Returns:
            The patient, their coverage and their active conditions.
        """
        patient, coverage, conditions = await asyncio.gather(
            self.get_patient(patient_id),
            self.get_coverage(patient_id),
            self.get_conditions(patient_id),
        )
        return PatientContext(
            patient=patient,
            coverage=coverage,
            conditions=conditions,
            requires_manual_confirmation=needs_manual_confirmation(coverage),
        )

    async def get_encounter_coverage_context(self, encounter_id: str) -> EncounterCoverageContext:
        """Composed. Everything ``encounters`` needs about payer and place.

        Added by TASK-052b, and the second composed method here. It exists
        because the three columns a policy query is keyed on come from two
        different resources: ``insurance_payer`` and ``insurance_plan_type``
        from the patient's ``Coverage``, and ``state`` from the encounter's own
        ``Location`` or ``Organization``. A caller assembling those itself would
        have to know that split, which is the knowledge this layer exists to
        hold.

        **A vendor subclass overrides this, not the primitives.** Same rule as
        ``get_patient_context()``: enrichment is about the assembled answer.

        The patient's id is read off ``Encounter.subject`` rather than taken as
        a parameter, so a caller cannot pair an encounter with someone else's
        coverage.

        Args:
            encounter_id: The encounter's id on this EHR.

        Returns:
            The payer half, the site-of-care state, and whether a provider must
            confirm the payer information by hand.
        """
        encounter = await self.get_encounter(encounter_id)
        patient_id = reference_id(encounter.subject, "Patient")

        if patient_id is None:
            # An encounter with no readable subject is not an error — it is an
            # encounter we cannot look up coverage for. The state still resolves,
            # because it comes off the encounter rather than off the patient.
            logger.warning(
                "Encounter has no resolvable Patient subject, so no Coverage can be "
                "read for it. Recording the site-of-care state alone and asking for "
                "manual confirmation of the payer."
            )
            return EncounterCoverageContext(
                encounter_id=encounter.id or encounter_id,
                patient_id=None,
                coverage=None,
                state=await self.resolve_site_of_care_state(encounter),
                requires_manual_confirmation=True,
            )

        # Independent reads against one server; serially they would cost three
        # round trips of latency for nothing. Same reasoning as
        # ``get_patient_context()``, and likewise without ``return_exceptions``.
        coverage, patient, state = await asyncio.gather(
            self.get_coverage(patient_id),
            self.get_patient(patient_id),
            self.resolve_site_of_care_state(encounter),
        )
        log_state_disagreement(state, patient.address_state)

        return EncounterCoverageContext(
            encounter_id=encounter.id or encounter_id,
            patient_id=patient_id,
            coverage=coverage,
            state=state,
            requires_manual_confirmation=needs_manual_confirmation(coverage),
        )

    async def resolve_site_of_care_state(self, encounter: Encounter) -> str | None:
        """Return the encounter's site-of-care state as a USPS code, or None.

        ``Encounter.location`` first, then ``Encounter.serviceProvider``, then
        nothing — the order and the deliberate absence of a patient-address
        fallback are argued in :mod:`src.adapters.site_of_care`.

        **A location that cannot be read is not fatal.** A dangling reference or
        a permission the launch does not carry falls through to the next
        candidate, because a coarser answer beats no answer. An outage
        propagates unchanged: ``FHIRUpstreamUnavailable`` is the one failure
        worth surfacing, since swallowing it would let "the EHR was down" look
        exactly like "this encounter has no location".

        Args:
            encounter: The encounter as the EHR holds it.

        Returns:
            The two-character USPS code, or None when nothing resolved one.
        """
        for location_id in site_location_references(encounter):
            try:
                location = await self.get_location(location_id)
            except (FHIRResourceNotFound, FHIRMalformedResponse, FHIRAuthorizationExpired):
                logger.warning(
                    "An Encounter.location could not be read; trying the next candidate. "
                    "The site-of-care state falls back to the service provider if none "
                    "resolves."
                )
                continue
            state = to_usps_state(location_state(location))
            if state is not None:
                return state

        organization_id = service_provider_reference(encounter)
        if organization_id is None:
            return None
        try:
            organization = await self.get_organization(organization_id)
        except (FHIRResourceNotFound, FHIRMalformedResponse, FHIRAuthorizationExpired):
            logger.warning(
                "The Encounter.serviceProvider Organization could not be read, and no "
                "Location resolved either. Leaving the encounter's state NULL rather "
                "than falling back to the patient's address, which is a different fact."
            )
            return None
        return to_usps_state(organization_state(organization))

    # -- Neither layer: a write and a submission ------------------------------

    async def write_clinical_note(self, note: ClinicalNoteContent) -> str:
        """Write a SOAP note back to the EHR as a ``DocumentReference``.

        The resource is composed by :func:`~src.adapters.note_document.build_document_reference`,
        which is where the note type, the required US Core category, the
        attestation status and — most importantly — the filter on which codes may
        leave this system all live. A vendor subclass that needs to adjust the
        write should call that builder and amend what it returns, exactly as
        Cerner and Epic are expected to call ``super().get_patient_context()``:
        rebuilding the resource by hand is how the filter gets lost.

        Args:
            note: The note, its two EHR identifiers, and whether a provider has
                attested to it. Its codes arrive unfiltered.

        Returns:
            The id of the created ``DocumentReference``, which the route records
            on ``clinical_notes.ehr_document_ref_id``.

        Raises:
            FHIRAuthorizationExpired: The EHR rejected the token (401/403).
            FHIRUpstreamUnavailable: Transport failure or a 5xx.
            FHIRMalformedResponse: The EHR refused the resource, or accepted it
                without saying what it created.
        """
        document = build_document_reference(note)
        return await self._create(
            "DocumentReference",
            document.model_dump(by_alias=True, exclude_none=True),
            resource_id=note.encounter_id,
        )

    async def submit_prior_auth(self, content: PriorAuthContent) -> PriorAuthSubmission:
        """Submit a prior authorization through FHIR ``Claim/$submit`` (Da Vinci PAS).

        Overridden in ``AthenaAdapter``, which has no FHIR PAS support and
        submits through CoverMyMeds instead (TASK-054).

        **This signature was corrected against the IG; the original encoded two
        mistakes.** TASK-050 typed it ``submit_prior_auth(bundle: Claim)`` from
        TASK-054's own wording, which was written before anyone opened the
        Implementation Guide — the same way the CRD "skip the RAG path entirely"
        claim was. What ``OperationDefinition/Claim-submit`` (PAS v2.2.1)
        actually specifies, at the type level on ``[base]/Claim/$submit``:

        * **in** — ``resource``, 1..1, a **Bundle** on
          ``profile-pas-request-bundle``: "A Bundle containing a single Claim
          plus referenced resources". ``Bundle.type`` is fixed to ``collection``,
          ``identifier`` and ``timestamp`` are both 1..1, and the ClaimFirst
          invariant puts the Claim in the first entry.
        * **out** — ``return``, 1..1, a **Bundle** on
          ``profile-pas-response-bundle`` holding a ``ClaimResponse``, or an
          ``OperationOutcome``. Never a bare ``ClaimResponse``.

        The second mistake was the larger one: a FHIR resource does not belong at
        this boundary at all. The parameter is normalized content and the builder
        composes the Bundle, exactly as :meth:`write_clinical_note` takes a
        :class:`~src.adapters.models.ClinicalNoteContent` and
        ``note_document.build_document_reference()`` composes the resource. No
        caller has ever held a ``Claim`` — ``prior_auth_requests`` stores
        procedures, diagnoses and evidence as JSONB — and the CoverMyMeds
        override needs those fields rather than a Bundle to take apart.

        Args:
            content: The request in this system's own terms. Codes arrive
                unfiltered; the builder applies the ``source`` filter, so no call
                site can forget it.

        Returns:
            What the payer said, its reference for the submission when it gave
            one, and which path submitted it.

        Raises:
            PriorAuthNotSubmittable: The request cannot be made into a conformant
                bundle, or the payer's answer carried no ``ClaimResponse``.
            FHIRAuthorizationExpired: The payer's endpoint rejected the token.
            FHIRUpstreamUnavailable: Transport failure or a 5xx.
            FHIRMalformedResponse: The submission was refused, or answered with
                something that is not a PAS response bundle.
        """
        request_bundle = build_request_bundle(content)
        body = await self._invoke(
            "Claim/$submit",
            request_bundle.model_dump(by_alias=True, exclude_none=True),
            resource_id=content.request_id,
        )

        try:
            response_bundle = Bundle.model_validate(body)
        except ValidationError as exc:
            # Deliberately not ``str(exc)``: Pydantic echoes the offending value,
            # and a payer's response bundle is full of patient data.
            raise FHIRMalformedResponse(
                "Bundle",
                content.request_id,
                "the payer's answer was not a PAS response bundle",
            ) from exc

        unknown = unknown_entry_types(response_bundle)
        if unknown:
            # Types this package does not model are conformant in a PAS response
            # — Practitioner, Task, OperationOutcome. Logged so an unexpected one
            # is visible, never acted on. Resource type names only: the entries
            # themselves are PHI.
            logger.info("PAS response carried unmodelled entry types: %s", ", ".join(unknown))

        claim_response = read_response_bundle(response_bundle)
        return PriorAuthSubmission(
            outcome=submission_outcome(claim_response),
            payer_reference_number=payer_reference_number(claim_response),
            submission_method=SubmissionMethod.FHIR_PAS,
        )


def needs_manual_confirmation(coverage: CoverageInfo | None) -> bool:
    """Return whether a provider must confirm the payer information by hand.

    The rule is TASK-052's enumerated table, in one function because TASK-052b
    writes three encounter columns straight off it and a second derivation is
    how two readings of one rule drift apart.

    True when there is no usable coverage at all, when the payer cannot be read,
    or when the plan type cannot be read. **A missing ``member_id`` does not set
    it**, deliberately: it is not a segment of the
    ``rag:{payer}:{plan_type}:{state}:{cpt_code}`` cache key and changes no
    policy answer. TASK-060's prior-auth bundle is what needs it, and that is
    far enough downstream to check for itself — asking a provider to confirm
    payer details in order to fix a field unrelated to the payer's policy would
    be noise.

    Args:
        coverage: The normalized coverage, or None when the EHR held none usable.

    Returns:
        True when the payer information is incomplete.
    """
    if coverage is None:
        return True
    return coverage.payer is None or coverage.plan_type is None
