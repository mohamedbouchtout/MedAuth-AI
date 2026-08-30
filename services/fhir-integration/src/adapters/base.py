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

Every method here is a stub. TASK-052 implements the fetches, TASK-053 the note
write-back and TASK-054 the prior authorization submission.
"""

from __future__ import annotations

from fhir_types import Claim, Condition, Encounter

from .models import CoverageInfo, PatientContext, PatientInfo, PriorAuthSubmission


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
    """

    def __init__(self, fhir_base_url: str, access_token: str) -> None:
        """Bind the adapter to one EHR's FHIR endpoint and one session's token.

        Args:
            fhir_base_url: Base URL of the EHR's FHIR R4 server.
            access_token: The SMART on FHIR access token for this session.
        """
        self.fhir_base_url = fhir_base_url
        self._access_token = access_token

    def __repr__(self) -> str:
        """Render the adapter without its access token. See the class docstring."""
        return f"{type(self).__name__}(fhir_base_url={self.fhir_base_url!r})"

    # -- Primitives: one resource type each, no composition -------------------

    async def get_patient(self, patient_id: str) -> PatientInfo:
        """Primitive. Read the patient's demographics.

        Args:
            patient_id: The patient's id on this EHR.

        Returns:
            The patient's demographics, flattened out of ``Patient``.
        """
        raise NotImplementedError("get_patient is implemented in TASK-052")

    async def get_coverage(self, patient_id: str) -> CoverageInfo | None:
        """Primitive. Read the patient's insurance coverage.

        Returns None when the EHR holds no usable ``Coverage`` at all. Partial
        coverage is not an error and must not be filled in with a guess — see
        ``PatientContext.requires_manual_confirmation``.

        Args:
            patient_id: The patient's id on this EHR.

        Returns:
            The payer, plan type and member id, or None.
        """
        raise NotImplementedError("get_coverage is implemented in TASK-052")

    async def get_conditions(self, patient_id: str) -> list[Condition]:
        """Primitive. Read the patient's active conditions.

        Args:
            patient_id: The patient's id on this EHR.

        Returns:
            The active ``Condition`` resources, empty when there are none.
        """
        raise NotImplementedError("get_conditions is implemented in TASK-052")

    async def get_encounter(self, encounter_id: str) -> Encounter:
        """Primitive. Read one encounter.

        Args:
            encounter_id: The encounter's id on this EHR.

        Returns:
            The ``Encounter`` resource.
        """
        raise NotImplementedError("get_encounter is implemented in TASK-052")

    # -- Composed: assembles primitives, and is the override point ------------

    async def get_patient_context(self, patient_id: str) -> PatientContext:
        """Composed. Assemble ``get_patient()``, ``get_coverage()`` and ``get_conditions()``.

        **This is the method a vendor subclass overrides**, because enrichment
        and fallback are about the assembled context rather than one fetch: Epic
        adds proprietary extensions to it, Cerner fills a payer field the
        ``Coverage`` fetch returned incomplete. An override calls ``super()``
        and adjusts what comes back — it does not reimplement the three fetches,
        which is what overriding a primitive would force.

        Args:
            patient_id: The patient's id on this EHR.

        Returns:
            The patient, their coverage and their active conditions.
        """
        raise NotImplementedError("get_patient_context is implemented in TASK-052")

    # -- Neither layer: a write and a submission ------------------------------

    async def write_clinical_note(
        self, encounter_id: str, note_text: str, icd10_codes: list[str]
    ) -> str:
        """Write a SOAP note back to the EHR as a ``DocumentReference``.

        Args:
            encounter_id: The encounter the note belongs to.
            note_text: The generated SOAP note.
            icd10_codes: The note's ICD-10-CM codes, dotted as stored.

        Returns:
            The id of the created ``DocumentReference``, which TASK-053 stores
            on ``clinical_notes.ehr_document_ref_id``.
        """
        raise NotImplementedError("write_clinical_note is implemented in TASK-053")

    async def submit_prior_auth(self, bundle: Claim) -> PriorAuthSubmission:
        """Submit a prior authorization through FHIR Claim/$submit (Da Vinci PAS).

        Overridden in ``AthenaAdapter``, which has no FHIR PAS support and
        submits through CoverMyMeds instead (TASK-054).

        Args:
            bundle: The assembled prior authorization ``Claim``.

        Returns:
            The payer's reference and which path submitted it.
        """
        raise NotImplementedError("submit_prior_auth is implemented in TASK-054")
