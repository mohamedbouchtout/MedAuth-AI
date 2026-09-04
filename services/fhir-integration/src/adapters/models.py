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
what TASK-052 and TASK-054 say they return. A later task fills them in and may
add to them — TASK-052 did exactly that, and TASK-054's builder is expected to.
"""

from __future__ import annotations

from enum import StrEnum

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
        address_state: The patient's residence state as a USPS code, when the
            resource carried one this vocabulary recognises. **Not a source for
            the encounter's ``state`` column**, and TASK-052b's only use of it
            is the disagreement warning: the policy documents scope themselves
            by the site of care, so a patient's residence is a different fact
            that merely coincides most of the time. Reading it as the site of
            care is the mistake this field's presence makes easy, which is why
            the ban is written on the field rather than only in
            :mod:`src.adapters.site_of_care`.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: str
    family_name: str | None = None
    given_names: list[str] = []
    birth_date: str | None = None
    gender: AdministrativeGender | None = None
    address_state: str | None = None


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


class PatientSearchMatch(BaseModel):
    """One candidate returned by a patient search.

    **Deliberately narrower than :class:`PatientInfo`.** A search answers with
    several patients at once, and every field in the response is disclosed for
    every one of them — including the people who are not the patient in the room.
    So this carries only what a provider needs to tell one candidate from
    another, which is the minimum-necessary principle applied to a list rather
    than to a record. ``address_state`` in particular is absent: it exists on
    ``PatientInfo`` for the site-of-care disagreement check and nothing on a
    picker screen would read it.

    Attributes:
        patient_id: The patient's id on this EHR — what the caller sends to
            ``POST /sessions/start`` once one is picked.
        family_name: Family name, when the EHR held one.
        given_names: Given names, in the order the EHR returned them.
        birth_date: ``Patient.birthDate`` as the EHR spelled it. Present because
            two people in one practice share a name far more often than they
            share a name and a date of birth, and picking the wrong one files an
            encounter against the wrong chart.
        gender: Administrative gender, when recorded.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: str
    family_name: str | None = None
    given_names: list[str] = []
    birth_date: str | None = None
    gender: AdministrativeGender | None = None


class PatientSearchResults(BaseModel):
    """What a patient search answers with.

    Attributes:
        matches: The candidates, capped. Empty is an ordinary answer — the EHR
            holds nobody by that name — and never an error.
        truncated: Whether the EHR had more matches than were returned. **Never
            silently dropped**: a provider shown five of two hundred Smiths and
            not told so will assume the person they want is not there. Same rule
            as the transcript-limit one in CLAUDE.md — chunk and merge, or report
            reduced coverage, but never truncate in silence.
    """

    model_config = ConfigDict(frozen=True)

    matches: list[PatientSearchMatch] = []
    truncated: bool = False


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


class EncounterCoverageContext(BaseModel):
    """What one encounter needs written onto its ``encounters`` row.

    TASK-052b. This is what ``EHRAdapter.get_encounter_coverage_context()``
    returns and what ``GET /fhir/encounter/{encounter_id}/coverage-context``
    answers with, and it maps one-to-one onto three columns:
    ``insurance_payer``, ``insurance_plan_type`` and ``state``.

    **It is a separate shape from :class:`PatientContext` because it answers a
    different question.** That one is "what do we know about this patient" and
    is keyed on a patient; this one is "which payer policy set applies to this
    visit, and where did it happen" and is keyed on an encounter. Only this one
    can carry ``state``, because the site of care is a property of the encounter
    and no patient-keyed shape has anywhere honest to put it.

    Attributes:
        encounter_id: The encounter's id on the EHR that answered.
        patient_id: The subject read off ``Encounter.subject``, or None when the
            reference could not be resolved — in which case no coverage was
            read either.
        coverage: The payer half, or None when the EHR held no usable
            ``Coverage``. Reuses :class:`CoverageInfo` rather than flattening
            it, so the enumerated rule in TASK-052 has exactly one shape to
            produce.
        state: The **site-of-care** state as a two-character USPS code, or None
            when neither a ``Location`` nor an ``Organization`` yielded one.
            Never the patient's residence — see
            :mod:`src.adapters.site_of_care` for what the policy documents
            actually say. Normalized through ``payer_vocab.normalize_state`` so
            it speaks the vocabulary ``insurance_policies.state`` is matched
            against.
        requires_manual_confirmation: True when the payer information is
            incomplete, by the same :func:`~src.adapters.base.needs_manual_confirmation`
            rule the patient context uses. **A NULL ``state`` does not set it**,
            deliberately: the flag is about payer details a provider can fill in
            from the patient's card, and nobody at the bedside can supply a site
            of care the EHR did not record. ``resolve_query_parameters()``
            already names ``state`` when it is missing, which is where that gap
            is visible.
    """

    model_config = ConfigDict(frozen=True)

    encounter_id: str
    patient_id: str | None = None
    coverage: CoverageInfo | None = None
    state: str | None = None
    requires_manual_confirmation: bool = False


class SubmissionMethod(StrEnum):
    """Which path a prior authorization went out by.

    **A mirror of ``track_a_clinical.models.SubmissionMethod``, which owns it.**
    That service owns the ``prior_auth_requests`` table this value is stored in,
    and this service holds ``medauth-track-a-clinical`` as a *dev* dependency
    only — nothing in ``src/`` imports it, and nothing should, for the reason
    ``tests/unit/test_note_contract.py`` gives about the two payload mirrors: a
    wire contract binds two services, and importing across the boundary would
    make a deployment of one require a redeploy of the other. The mirror is the
    deliberate choice and ``tests/unit/test_prior_auth_contract.py`` is the price
    of it — the two definitions are proven equal rather than assumed so.
    """

    #: Da Vinci PAS — ``POST [base]/Claim/$submit``. The base adapter's path.
    FHIR_PAS = "fhir-pas"

    #: CoverMyMeds, for an EHR or payer with no FHIR PAS support.
    COVERMYMEDS = "covermymeds"

    #: Fax. Routed by TASK-061 in ``prior-auth``, never by this service.
    FAX = "fax"


class SubmissionOutcome(StrEnum):
    """What the payer said, normalized across both submission paths.

    **Not a FHIR code, despite sharing PAS's spellings.** The members are taken
    from ``ClaimResponse.outcome``'s required binding in the PAS IG, because that
    is a real four-way distinction someone already thought through rather than
    one invented here. But CoverMyMeds is not FHIR and answers in its own terms,
    so this is a normalized shape the two paths both map onto — the same
    relationship :class:`CoverageInfo` has to a FHIR ``Coverage``. TASK-004b's
    ``ClaimResponseOutcome`` literal is what the PAS *parser* reads before
    mapping onto this.

    **The CoverMyMeds side of that mapping is unverified** — see
    ``adapters/athena.py``. The PAS side is read straight off the IG.

    Carrying this at all is the point. A submission is not a decision, and a
    result carrying only a reference number would let a caller record
    ``status='submitted'`` for a request the payer rejected outright or has not
    looked at — the same failure as reading a payer's silence as "no
    authorization required", one layer down.
    """

    #: The payer adjudicated the request. Says nothing about *which way* — an
    #: approval and a denial are both complete, and the denial detail is on the
    #: response, not here.
    COMPLETE = "complete"

    #: The payer accepted the request and has not decided. Ordinary and
    #: conformant, not an error, and the state in which ``preAuthRef`` is most
    #: often absent.
    QUEUED = "queued"

    #: Some items were adjudicated and others were not.
    PARTIAL = "partial"

    #: The payer refused to process the request — malformed, or missing
    #: something it required. Nothing was authorized and nothing is pending.
    ERROR = "error"


class PriorAuthSubmission(BaseModel):
    """What came back from submitting a prior authorization, whichever path took it.

    Attributes:
        outcome: What the payer said. Required, because every path has an answer
            and dropping it would let a caller record a rejection as a
            submission.
        payer_reference_number: The payer's reference for the submission, when it
            returned one — ``ClaimResponse.preAuthRef``, which is 0..1 at the
            root and "only present on preauthorization adjudications". Its
            absence on a queued answer is normal and is not a failure.
        submission_method: Which path submitted it.
    """

    model_config = ConfigDict(frozen=True)

    outcome: SubmissionOutcome
    payer_reference_number: str | None = None
    submission_method: SubmissionMethod


class NoteCode(BaseModel):
    """One extracted clinical code, as ``track-a-clinical`` stores and returns it.

    A trimmed mirror of CLAUDE.md's "Extracted clinical codes" contract, holding
    only what a chart write needs. ``source`` is required rather than optional
    because it is the field that decides whether the code may be written at all —
    an entry without one is malformed, not a code of unknown provenance to be
    sent anyway. See :data:`~src.adapters.outbound_codes.SENDABLE_CODE_SOURCES`.

    Attributes:
        code: The code itself, dotted as stored (``M17.11``, not ``M1711``).
        display: The proposing source's own description, when it carried one.
        source: ``llm-extraction``, ``comprehend-medical`` or
            ``provider-accepted``.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    code: str
    display: str | None = None
    source: str


class ClinicalNoteContent(BaseModel):
    """One note and the two identifiers that say which chart entry it belongs on.

    What :meth:`~src.adapters.base.EHRAdapter.write_clinical_note` takes. It is
    one argument rather than five so that a vendor subclass overriding the write
    — Athenahealth is the likely first — changes one signature rather than
    re-listing the parameters, the same reasoning that makes
    ``get_patient_context()`` the override point rather than its primitives.

    **The codes arrive unfiltered.** Filtering is the writer's obligation and it
    happens inside the document builder, so a caller cannot forget it and a
    subclass that reuses the builder inherits it. See CLAUDE.md, "Writing
    clinical data out to the EHR".

    Attributes:
        patient_id: The patient as the EHR knows them — the document's subject.
        encounter_id: The encounter as the EHR knows it. Not a ``session_id``,
            which names the same visit in our namespace and would address
            nothing on a chart.
        subjective: The note's subjective section, when one was generated.
        objective: The objective section.
        assessment: The assessment section.
        plan: The plan section.
        icd10_codes: Every extracted diagnosis, whatever its source, or None
            when the extraction pass never answered.
        reviewed_by_provider: Whether a provider has attested to the note. It
            decides the written document's ``docStatus`` and nothing else; no
            write-back ever sets this flag.
    """

    model_config = ConfigDict(frozen=True)

    patient_id: str
    encounter_id: str
    subjective: str | None = None
    objective: str | None = None
    assessment: str | None = None
    plan: str | None = None
    icd10_codes: list[NoteCode] | None = None
    reviewed_by_provider: bool = False


class PriorAuthProcedure(BaseModel):
    """One procedure a prior authorization is being requested for.

    Taken from ``prior_auth_requests.procedures``, which TASK-060 fills from the
    nudges fired during the encounter — so ``description`` is the procedure as
    the clinician said it, and ``cpt_code`` is what it resolved to.

    Attributes:
        cpt_code: The five-character CPT code, uppercased and stripped as the
            code contract requires.
        description: The procedure in the clinician's own words, when one was
            recorded. Not a canonical descriptor.
    """

    model_config = ConfigDict(frozen=True)

    cpt_code: str
    description: str | None = None


class PriorAuthEvidence(BaseModel):
    """One excerpt of clinical documentation supporting the request.

    Becomes a ``Claim.supportingInfo`` entry, which is where a payer's criteria
    are actually evaluated — "a missing one is the usual cause of a denial", per
    ``fhir_types.claim``.

    **These are transcript excerpts and they are PHI.** They are excerpts rather
    than the whole transcript by a HIPAA minimum-necessary decision made in
    TASK-060, not to keep the payload small; nothing downstream may widen them
    back out.

    Attributes:
        text: The excerpt itself.
        criterion: The payer criterion this excerpt is offered against, when the
            gap analysis tied it to one.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    criterion: str | None = None


class PriorAuthContent(BaseModel):
    """One prior authorization request, in this system's own terms.

    What :meth:`~src.adapters.base.submit_prior_auth` takes. **Not a FHIR
    resource, and deliberately so** — this is the same decision TASK-053 made for
    :class:`ClinicalNoteContent`, and TASK-054 records why it applies here too:
    the caller has never held a ``Claim`` (``prior_auth_requests`` stores
    procedures, diagnoses and evidence as JSONB), and the CoverMyMeds override
    needs these fields rather than a PAS ``Bundle`` it would have to take apart
    again. The FHIR composition happens inside the base adapter's builder, which
    is what keeps the profile's required elements and the ``source`` filter
    somewhere a call site cannot skip and a subclass inherits by reuse.

    **The codes arrive unfiltered**, exactly as they do on
    :class:`ClinicalNoteContent`: filtering is the writer's obligation and it
    happens in the builder. See CLAUDE.md, "Writing clinical data out to the
    EHR" — a ``comprehend-medical`` code is a machine's suggestion, and a bundle
    asserts to a payer what the provider documented.

    **The fields are what ``prior_auth_requests`` holds plus the two EHR
    identifiers**, which is what can be grounded today. TASK-054's builder is
    expected to extend this — the module docstring above says as much, and
    TASK-052 already did it once. One gap is known and named rather than guessed
    at: the PAS request profile requires ``Claim.insurance``, which references a
    ``Coverage`` **resource**, and nothing in this repository carries a coverage
    resource id — :class:`CoverageInfo` flattens a payer name, plan type and
    member id out of one and discards its identity. Closing that is TASK-054's,
    and it must not be closed by inventing a reference.

    Attributes:
        request_id: The ``prior_auth_requests`` row this came from. Carried so
            the result can be recorded against it without the route holding a
            second copy of the identifier.
        patient_id: The patient as the EHR knows them — the Claim's subject.
        encounter_id: The encounter as the EHR knows it. Not a ``session_id``,
            which names the same visit in our namespace and would address
            nothing a payer or a chart can resolve.
        provider_reference: The requesting provider, as the EHR asserted them at
            launch — the same verified ``Practitioner`` reference that goes in
            ``audit_log.fhir_practitioner_ref``. Never ``encounters.provider_id``,
            which is a UUID this system minted and which identifies nobody to a
            payer. None when the launch never yielded a verified one, which the
            builder has to answer for rather than fabricate.
        payer_name: The payer's own display name, as the coverage spelled it.
            Never a ``payer_vocab`` slug: a slug is for matching our own indexed
            policies, and this value is going to the payer.
        coverage: The payer half of the patient's context, for the member id a
            payer matches the request on.
        procedures: What is being requested. Empty is not a valid request.
        icd10_codes: The diagnoses justifying it, whatever their source, or None
            when the extraction pass never answered. ``None`` and ``[]`` mean
            different things here exactly as they do on the note.
        clinical_evidence: The documentation offered against the payer's
            criteria.
    """

    model_config = ConfigDict(frozen=True)

    request_id: str
    patient_id: str
    encounter_id: str
    provider_reference: str | None = None
    payer_name: str | None = None
    coverage: CoverageInfo | None = None
    procedures: list[PriorAuthProcedure] = []
    icd10_codes: list[NoteCode] | None = None
    clinical_evidence: list[PriorAuthEvidence] = []
