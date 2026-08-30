"""Normalized shapes the adapter layer returns, whatever EHR answered.

These are deliberately not FHIR resources. A ``Coverage`` from Athenahealth and
one from Cerner carry the payer in different places, and the point of the
adapter layer is that nothing above it has to know which. So the primitives that
read a vendor's resources return these, and ``fhir_types``' R4 models stay on
the wire side of the adapter.

Every field here is PHI. Nothing in this module may reach a log line, and a
service reading any of it records the access through hipaa-logger's
``audit_log()``.

The fields are the ones TASK-050 needs in order to type the stubs, drawn from
what TASK-052 and TASK-054 say they return. TASK-052 fills them in and may add
to them; nothing here is populated yet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from fhir_types import AdministrativeGender, Condition


class PatientInfo(BaseModel):
    """A patient's demographics, flattened out of the FHIR ``Patient`` resource.

    Attributes:
        patient_id: The patient's id on the EHR that answered.
        family_name: Family name, when the resource carried one.
        given_names: Given names, in the order the resource listed them.
        birth_date: Date of birth as a FHIR ``date`` string, when known.
        gender: Administrative gender, used for record matching and for the
            demographics a payer rule may key on (TASK-059). Not a statement
            about gender identity — see ``fhir_types.Patient``.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: str
    family_name: str | None = None
    given_names: list[str] = []
    birth_date: str | None = None
    gender: AdministrativeGender | None = None


class CoverageInfo(BaseModel):
    """The payer half of a patient's context, flattened out of ``Coverage``.

    ``payer`` is the payer's own display name, kept as the resource spelled it.
    It is normalized to a slug by ``/policies/query`` through
    ``payer_vocab.normalize_payer()``, which is the single normalisation site —
    do not slug it here as well. See CLAUDE.md, "Payer and jurisdiction
    identity", and TASK-052b, which is what writes these onto the encounter row.

    Attributes:
        payer: The payer's own display name, as spelled by the resource.
        plan_type: The plan type, from ``Coverage.type`` or ``Coverage.class``.
        member_id: The patient's member id with the payer.
    """

    model_config = ConfigDict(frozen=True)

    payer: str | None = None
    plan_type: str | None = None
    member_id: str | None = None


class PatientContext(BaseModel):
    """Everything a policy query needs about one patient, assembled from three fetches.

    This is what ``EHRAdapter.get_patient_context()`` returns and what
    ``GET /fhir/patient/{patient_id}/context`` answers with.

    Attributes:
        patient: The patient's demographics.
        coverage: The payer half, or None when the EHR returned no usable
            ``Coverage`` at all.
        conditions: The patient's active conditions, as FHIR R4 resources.
        requires_manual_confirmation: True when the payer information is
            incomplete. TASK-052 sets it rather than failing the request: a
            provider filling the payer in is a working encounter, and a guessed
            payer is a cache key standing for a plan the patient is not on.
    """

    model_config = ConfigDict(frozen=True)

    patient: PatientInfo
    coverage: CoverageInfo | None = None
    conditions: list[Condition] = []
    requires_manual_confirmation: bool = False


class PriorAuthSubmission(BaseModel):
    """What came back from submitting a prior authorization, whichever path took it.

    Athenahealth does not support FHIR PAS and submits through CoverMyMeds
    instead (TASK-054), so the two things worth recording are the payer's own
    reference and which path was used.

    Attributes:
        payer_reference_number: The payer's reference for the submission, when
            it returned one.
        submission_method: Which path submitted it. TASK-054 fixes the
            vocabulary when it implements the second path; it is a plain string
            until there is more than one value for it to be wrong about.
    """

    model_config = ConfigDict(frozen=True)

    payer_reference_number: str | None = None
    submission_method: str
