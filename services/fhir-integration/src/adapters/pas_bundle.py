"""Composing the Da Vinci PAS request bundle, and reading the response bundle.

TASK-054, and the counterpart of ``note_document.py`` for the other outbound
writer. Kept out of ``base.py`` for the same reason that one is: what lives here
is the profile's rules and this repository's rules, not HTTP plumbing, and both
are easier to find — and harder to quietly drop — under their own names.

**What ``Claim/$submit`` actually takes**, read off ``OperationDefinition/
Claim-submit`` in PAS v2.2.1 rather than assumed. It is a *type-level* operation
at ``[base]/Claim/$submit``. Its ``resource`` parameter is 1..1 and is a
``Bundle`` on ``profile-pas-request-bundle`` — "a Bundle containing a single
Claim plus referenced resources" — which fixes ``Bundle.type`` to ``collection``,
requires ``identifier`` and ``timestamp``, prohibits ``search``/``request``/
``response`` on an entry, and carries the **ClaimFirst** invariant: the Claim is
the first entry. It answers with a ``Bundle`` on
``profile-pas-response-bundle`` holding a ``ClaimResponse``, never a bare
``ClaimResponse``.

**The Coverage is composed into the bundle rather than referenced by id.** The
profile requires ``Claim.insurance``, which references a ``Coverage``, and
nothing in this repository holds a ``Coverage`` resource id —
:class:`~src.adapters.models.CoverageInfo` flattens a payer name, plan type and
member id out of one and discards its identity. TASK-054 says that gap "must not
be closed by inventing a reference", so it is closed the way a bundle is meant
to close it: the Coverage travels *in* the bundle under a ``urn:uuid`` full URL
that names it within this submission only, built from coverage data actually
read from the EHR. Nothing here claims an id exists on anybody's server.

**What this bundle does not carry, stated rather than left to be discovered.**
The profile expects the *referenced resources* to travel with the Claim, and the
Patient, Practitioner and Organization it references are not among the entries:
this service holds their identifiers, not their resources, and fetching and
embedding three US Core resources is real work with real extra PHI reads. They
are referenced relatively — ``Patient/{id}`` — which is honest about what we
know. Closing it is TASK-054b, gated on a real payer PAS endpoint to validate
against rather than on guesswork.

Everything in this module is PHI. Nothing here logs.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Final

from fhir_types import (
    Bundle,
    BundleEntry,
    Claim,
    ClaimDiagnosis,
    ClaimInsurance,
    ClaimItem,
    ClaimResponse,
    ClaimSupportingInfo,
    CodeableConcept,
    Coding,
    Coverage,
    Identifier,
    Reference,
    UnknownResource,
)

from .models import PriorAuthContent, SubmissionOutcome
from .outbound_codes import sendable_codes

#: ``Bundle.identifier`` is 1..1 on the request profile and has to be unique per
#: submission. RFC 3986 is the system FHIR itself uses for a URI-shaped business
#: identifier, and a fresh UUID is the value — nothing about a request is stable
#: enough to key on, and reusing one across submissions would let a payer treat
#: two requests as one.
IDENTIFIER_SYSTEM: Final = "urn:ietf:rfc:3986"

CLAIM_TYPE_SYSTEM: Final = "http://terminology.hl7.org/CodeSystem/claim-type"
#: ``professional`` is the type for a physician's office services, which is what
#: this platform records. Not ``institutional``, which is a facility's own claim.
CLAIM_TYPE_CODE: Final = "professional"

PROCESS_PRIORITY_SYSTEM: Final = "http://terminology.hl7.org/CodeSystem/processpriority"
#: ``normal`` rather than ``stat``. Nothing in this system knows whether a
#: request is urgent, and asserting urgency we were never told would spend the
#: attention of the one queue that exists for genuinely urgent requests.
PROCESS_PRIORITY_CODE: Final = "normal"

CPT_SYSTEM: Final = "http://www.ama-assn.org/go/cpt"
ICD10_SYSTEM: Final = "http://hl7.org/fhir/sid/icd-10-cm"

CLAIM_INFORMATION_CATEGORY_SYSTEM: Final = (
    "http://terminology.hl7.org/CodeSystem/claiminformationcategory"
)
#: The category for free-text clinical justification. ``info`` is the general
#: member of that value set, and this system has no finer classification of an
#: excerpt to assert.
CLAIM_INFORMATION_CATEGORY_CODE: Final = "info"


class PriorAuthNotSubmittable(Exception):
    """The request cannot be turned into a conformant PAS bundle.

    Raised rather than sending something the payer will refuse, and rather than
    filling a required element with a value nobody asserted. Every case names a
    fact that is genuinely missing:

    * no procedure to request authorization for,
    * no requesting provider — ``Claim.provider`` is 1..1, and the reference
      comes from the SMART launch's verified ``fhirUser``. When the launch never
      yielded one, the honest answer is that we cannot say who is asking. The
      alternative would be ``encounters.provider_id``, a UUID this system minted
      that identifies nobody to a payer.

    Attributes:
        reason: A fixed description of what is missing. It reaches a client, so
            it names the absent fact and never a value from the request.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now() -> str:
    """The current UTC instant, as FHIR spells one."""
    return datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")


def build_coverage(content: PriorAuthContent, patient_reference: str) -> Coverage:
    """Compose the ``Coverage`` the Claim is submitted against.

    Built from what the EHR's own ``Coverage`` gave us — the payer's display
    name, the plan type and the member id — rather than fetched, because the
    identity of the resource those fields came from was not kept. See the module
    docstring for why that is closed this way and not by inventing an id.

    ``payor`` is 1..1 and references an ``Organization``; with no organization
    resource for the payer, the reference carries the payer's own name as its
    ``display``. That is what we actually know, and a payer reading its own name
    is the point of the element.

    Args:
        content: The request in this system's terms.
        patient_reference: How the Claim refers to the patient, reused here so
            the two resources point at the same subject.

    Returns:
        The Coverage resource, to be carried in the bundle.
    """
    coverage_info = content.coverage
    return Coverage(
        status="active",
        beneficiary=Reference(reference=patient_reference),
        subscriber_id=coverage_info.member_id if coverage_info else None,
        payor=[Reference(display=content.payer_name)],
        type=(
            CodeableConcept(text=coverage_info.plan_type)
            if coverage_info and coverage_info.plan_type
            else None
        ),
    )


def build_claim(
    content: PriorAuthContent, *, patient_reference: str, coverage_reference: str
) -> Claim:
    """Compose the prior-authorization ``Claim``.

    ``use`` is ``preauthorization``: this asks the payer to authorize a service
    before it happens, rather than billing for one that has.

    **The diagnoses are filtered on ``source``.** A ``comprehend-medical`` entry
    is a code the validating pass surfaced that no provider ever stated, and a
    bundle asserts to a payer what the provider documented. The filter is
    :func:`~src.adapters.outbound_codes.sendable_codes`, applied here rather than
    at a call site so no caller can forget it and a vendor subclass reusing this
    builder inherits it. See CLAUDE.md, "Writing clinical data out to the EHR".

    **Every item points at every diagnosis and every piece of evidence.** The
    stored request carries no mapping from one procedure to the particular
    diagnosis that justifies it, so a narrower assertion would be invented.
    Pointing at all of them says what the request actually means: these are the
    diagnoses and this is the documentation offered for what is being asked.

    Args:
        content: The request in this system's terms, codes unfiltered.
        patient_reference: How to refer to the patient.
        coverage_reference: How to refer to the Coverage in this bundle.

    Returns:
        The Claim resource, to be the bundle's first entry.

    Raises:
        PriorAuthNotSubmittable: No procedure, or no requesting provider.
    """
    if not content.procedures:
        raise PriorAuthNotSubmittable("the request names no procedure to seek authorization for")
    if content.provider_reference is None:
        raise PriorAuthNotSubmittable(
            "the SMART launch yielded no verified provider, and a payer request "
            "cannot say who is asking"
        )

    diagnoses = [
        ClaimDiagnosis(
            sequence=index,
            diagnosis_codeable_concept=CodeableConcept(
                coding=[Coding(system=ICD10_SYSTEM, code=code.code, display=code.display)]
            ),
        )
        for index, code in enumerate(sendable_codes(content.icd10_codes), start=1)
    ]
    supporting_info = [
        ClaimSupportingInfo(
            sequence=index,
            category=CodeableConcept(
                coding=[
                    Coding(
                        system=CLAIM_INFORMATION_CATEGORY_SYSTEM,
                        code=CLAIM_INFORMATION_CATEGORY_CODE,
                    )
                ]
            ),
            value_string=evidence.text,
            reason=CodeableConcept(text=evidence.criterion) if evidence.criterion else None,
        )
        for index, evidence in enumerate(content.clinical_evidence, start=1)
    ]
    items = [
        ClaimItem(
            sequence=index,
            product_or_service=CodeableConcept(
                coding=[
                    Coding(
                        system=CPT_SYSTEM,
                        code=procedure.cpt_code,
                        display=procedure.description,
                    )
                ],
                text=procedure.description,
            ),
            diagnosis_sequence=[entry.sequence for entry in diagnoses] or None,
            information_sequence=[entry.sequence for entry in supporting_info] or None,
        )
        for index, procedure in enumerate(content.procedures, start=1)
    ]

    return Claim(
        status="active",
        type=CodeableConcept(coding=[Coding(system=CLAIM_TYPE_SYSTEM, code=CLAIM_TYPE_CODE)]),
        use="preauthorization",
        patient=Reference(reference=patient_reference),
        created=_now(),
        insurer=Reference(display=content.payer_name),
        provider=Reference(reference=content.provider_reference),
        priority=CodeableConcept(
            coding=[Coding(system=PROCESS_PRIORITY_SYSTEM, code=PROCESS_PRIORITY_CODE)]
        ),
        diagnosis=diagnoses or None,
        supporting_info=supporting_info or None,
        insurance=[
            ClaimInsurance(
                sequence=1,
                focal=True,
                coverage=Reference(reference=coverage_reference),
            )
        ],
        item=items,
    )


def build_request_bundle(content: PriorAuthContent) -> Bundle:
    """Build the ``Bundle`` to POST to ``Claim/$submit``.

    Satisfies the four things ``profile-pas-request-bundle`` requires of the
    bundle itself — ``type`` fixed to ``collection``, ``identifier`` and
    ``timestamp`` both present, and the Claim as the **first** entry — and
    carries the Coverage the Claim references as a second entry under a
    ``urn:uuid`` full URL.

    Args:
        content: The request in this system's terms. Codes arrive unfiltered;
            :func:`build_claim` filters them.

    Returns:
        The bundle to send. Not yet sent —
        :meth:`~src.adapters.base.EHRAdapter.submit_prior_auth` does that.

    Raises:
        PriorAuthNotSubmittable: The request is missing something a conformant
            bundle requires.
    """
    patient_reference = f"Patient/{content.patient_id}"
    coverage_full_url = f"urn:uuid:{uuid.uuid4()}"

    claim = build_claim(
        content,
        patient_reference=patient_reference,
        coverage_reference=coverage_full_url,
    )
    coverage = build_coverage(content, patient_reference)

    return Bundle(
        type="collection",
        identifier=Identifier(system=IDENTIFIER_SYSTEM, value=f"urn:uuid:{uuid.uuid4()}"),
        timestamp=_now(),
        entry=[
            # ClaimFirst. The invariant is the profile's, and the order of this
            # list is the whole of its implementation — which is why a test
            # asserts it on the bytes that go on the wire rather than here.
            BundleEntry(full_url=f"urn:uuid:{uuid.uuid4()}", resource=claim),
            BundleEntry(full_url=coverage_full_url, resource=coverage),
        ],
    )


def read_response_bundle(bundle: Bundle) -> ClaimResponse:
    """Pull the ``ClaimResponse`` out of what a payer answered.

    The operation returns a bundle rather than the resource, so the response has
    to be looked into. An entry of a type this package does not model arrives as
    an :class:`~fhir_types.UnknownResource` and is simply not the one we want —
    which is the reason ``Bundle.entry.resource`` tolerates unmodelled types at
    all: a PAS response legitimately carries ``Practitioner``, ``Task`` and
    ``OperationOutcome`` entries alongside the answer.

    Args:
        bundle: The parsed response bundle.

    Returns:
        The first ``ClaimResponse`` entry.

    Raises:
        PriorAuthNotSubmittable: The bundle carries no ClaimResponse. That is a
            malformed answer rather than a refusal — an ``OperationOutcome``-only
            response says the payer could not process the request, and the caller
            turns it into an upstream error rather than recording an outcome the
            payer never gave.
    """
    for entry in bundle.entry or ():
        if isinstance(entry.resource, ClaimResponse):
            return entry.resource
    raise PriorAuthNotSubmittable("the payer's response carried no ClaimResponse")


def submission_outcome(response: ClaimResponse) -> SubmissionOutcome:
    """Map ``ClaimResponse.outcome`` onto this system's normalized answer.

    A straight rename rather than a judgement: the members are the same four,
    because they were taken from this binding in the first place. It exists as a
    function so the mapping has one site, the way the CoverMyMeds side's does.

    ``outcome`` is 1..1 with a required binding, so a conformant response always
    has one and there is no default to fall back to.

    Args:
        response: The payer's ClaimResponse.

    Returns:
        What the payer said, normalized.
    """
    return SubmissionOutcome(response.outcome)


def payer_reference_number(response: ClaimResponse) -> str | None:
    """Return the authorization number the payer issued, if it issued one.

    ``preAuthRef`` is 0..1 at the ClaimResponse root and is only present on an
    adjudicated preauthorization, so ``None`` is an ordinary answer on a queued
    response and not a sign that anything failed.

    Args:
        response: The payer's ClaimResponse.

    Returns:
        The payer's reference, or None.
    """
    return response.pre_auth_ref


def unknown_entry_types(bundle: Bundle) -> list[str]:
    """Return the resource types in a response this package does not model.

    Used for an operational log line, never for a decision: a PAS response is
    conformant with ``Practitioner``, ``Task`` and ``OperationOutcome`` entries,
    and this exists so an unexpected one is visible rather than silently
    discarded.

    Args:
        bundle: The parsed response bundle.

    Returns:
        The distinct unmodelled resource types, in the order they appear.
    """
    seen: list[str] = []
    for entry in bundle.entry or ():
        resource = entry.resource
        if isinstance(resource, UnknownResource):
            resource_type = resource.resource_type
            if resource_type is not None and resource_type not in seen:
                seen.append(resource_type)
    return seen
