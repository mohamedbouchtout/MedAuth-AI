"""Request and response bodies for the policy endpoints.

The ingest request arrives as ``multipart/form-data`` — a PDF plus its metadata
— so its fields come through as strings. Two consequences are handled here
rather than in the route: an omitted optional form field arrives as the empty
string rather than as absent, and ``state`` needs normalising to the two
uppercase characters the ``CHAR(2)`` column stores.

The uploaded file is a field *of the request model* rather than a separate route
parameter, which is not a stylistic choice: FastAPI only flattens a form model
into its individual fields when that model is the sole body parameter. Declaring
``UploadFile`` alongside it makes FastAPI look for a form field literally named
``metadata``, and every well-formed request fails validation. Keeping the file
inside the model keeps the flattening — and annotating the parameter with
``File()`` rather than ``Form()`` at the route is what makes the published spec
say ``multipart/form-data`` instead of ``application/x-www-form-urlencoded``.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any, Literal

from fastapi import UploadFile
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from payer_vocab import normalize_payer
from track_b_rag.documents import DEFAULT_CONTENT_TYPE, ContentType
from track_b_rag.policy_rules import RulesSource


def _empty_to_none(value: object) -> object:
    """Treat an empty or blank form field as absent.

    A multipart client that renders an unset field as ``name=""`` should mean
    "no value", not "the empty string" — which would otherwise fail a
    ``min_length`` check or store an empty ``plan_type``.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _upper(value: object) -> object:
    """Uppercase a state code, leaving anything else for the validator to reject."""
    return value.upper() if isinstance(value, str) else value


OptionalFormStr = Annotated[str | None, BeforeValidator(_empty_to_none)]
OptionalFormDate = Annotated[datetime.date | None, BeforeValidator(_empty_to_none)]
StateCode = Annotated[str | None, BeforeValidator(_empty_to_none), BeforeValidator(_upper)]


def _upper_codes(value: object) -> object:
    """Uppercase a list of state codes, leaving anything else for the validator."""
    if isinstance(value, list):
        return [item.upper() if isinstance(item, str) else item for item in value]
    return value


#: A contractor jurisdiction: the states a Medicare LCD applies in. Repeated
#: form fields rather than one delimited string, so the wire format needs no
#: parsing convention of its own.
JurisdictionStates = Annotated[list[str], BeforeValidator(_upper_codes)]

#: The query endpoint takes JSON rather than a form, so an omitted field is
#: absent rather than empty and only the case normalisation applies. It matches
#: what ingestion stores, which is what makes the Qdrant filter and the cache key
#: agree about which payer and which state a request means.
RequiredStateCode = Annotated[str, BeforeValidator(_upper)]
CptCode = Annotated[str, BeforeValidator(_upper)]


def _must_be_normalizable(value: str) -> str:
    """Reject a payer name that cannot produce a slug, and keep the rest as sent.

    The stored and matched form is the slug (TASK-016), but the payer's own
    spelling is what goes into the `insurance_policies` row, so the field keeps
    the raw value and the models below leave normalisation to the call sites
    that need it. What this validator adds is the boundary check: a "name" made
    only of punctuation has no slug, and finding that out here returns a 422
    naming the field rather than a 500 from somewhere deeper in.
    """
    normalize_payer(value)
    return value


#: A payer name in any spelling. Both endpoints accept whatever the caller has —
#: "Aetna", "AETNA, Inc.", "Medicare Part B" — because both sides resolve it
#: through the same vocabulary before anything is matched or keyed on it.
PayerName = Annotated[str, AfterValidator(_must_be_normalizable)]


class IngestPolicyRequest(BaseModel):
    """Body of ``POST /policies/ingest`` — the document and its metadata.

    ``source_url`` and ``effective_date`` are not in TASK-011's four-field list,
    but the ``insurance_policies`` table has both columns and the scraper
    (TASK-013) knows both values at the point it calls this endpoint. They are
    optional parameters rather than columns permanently filled with NULL — the
    same call CLAUDE.md makes for hipaa-logger's ``ip_address``/``user_agent``.
    """

    model_config = ConfigDict(extra="forbid")

    policy_id: str = Field(
        min_length=1,
        max_length=200,
        description="The payer's own identifier for the document. Unique per policy.",
    )
    payer: PayerName = Field(
        min_length=1,
        max_length=200,
        description=(
            "Issuing payer, e.g. 'Aetna' or 'CMS'. Recorded as sent; indexed under "
            "its canonical slug so any spelling of the same payer retrieves it."
        ),
    )
    plan_type: OptionalFormStr = Field(
        default=None,
        max_length=100,
        description="Plan type the policy applies to, e.g. 'PPO'. Omit if it applies to all.",
    )
    state: StateCode = Field(
        default=None,
        pattern=r"^[A-Z]{2}$",
        description="Two-letter state code, or omitted for a policy that applies nationally.",
    )
    source_url: OptionalFormStr = Field(
        default=None,
        description="Where the document was retrieved from, recorded for provenance.",
    )
    effective_date: OptionalFormDate = Field(
        default=None,
        description="The payer's stated effective date for this version of the policy.",
    )
    jurisdiction_states: JurisdictionStates = Field(
        default_factory=list,
        description=(
            "USPS state codes for a policy issued per contractor jurisdiction, as "
            "repeated fields. Use this or `state`, not both; omit both for a "
            "national policy."
        ),
    )
    content_type: ContentType = Field(
        default=DEFAULT_CONTENT_TYPE,
        description=(
            "The format of the uploaded document. Defaults to PDF, which is what "
            "payers publish; CMS publishes HTML and offers no PDF at all."
        ),
    )
    file: UploadFile = Field(description="The policy document, in the declared format.")

    @field_validator("jurisdiction_states", mode="after")
    @classmethod
    def _codes_are_two_letters(cls, value: list[str]) -> list[str]:
        """Each entry is a USPS code. The `state` field's pattern cannot reach
        inside a list, so the same check is spelled out here."""
        for code in value:
            if len(code) != 2 or not code.isalpha():
                raise ValueError("Each jurisdiction state must be a two-letter code.")
        return value

    @model_validator(mode="after")
    def _one_way_of_saying_where(self) -> IngestPolicyRequest:
        """Reject a document that names both a state and a jurisdiction.

        They answer the same question and a caller setting both has two ideas
        about where the policy applies. Picking one silently would store the
        loser nowhere and leave retrieval quietly narrower than the caller
        believes.
        """
        if self.state and self.jurisdiction_states:
            raise ValueError(
                "Set either state or jurisdiction_states, not both: a policy applies "
                "in one state or across a contractor jurisdiction."
            )
        return self


class IngestPolicyData(BaseModel):
    """``data`` payload returned by ``POST /policies/ingest``.

    ``chunks_indexed`` is what this call wrote, so it is 0 for an ``unchanged``
    result — that is the signal the document was skipped, not an indication the
    collection holds nothing for it.
    """

    policy_id: str
    status: Literal["created", "updated", "unchanged"] = Field(
        description=(
            "'created' for a new policy_id, 'updated' when the content hash changed, "
            "'unchanged' when the document was already indexed."
        ),
    )
    content_hash: str = Field(description="SHA-256 hex digest of the uploaded PDF bytes.")
    chunks_indexed: int = Field(
        ge=0,
        description="Chunks written by this call. Zero when the document was unchanged.",
    )
    collection: str = Field(description="Qdrant collection the chunks were written to.")


class PolicyQueryRequest(BaseModel):
    """Body of ``POST /policies/query`` — one procedure, one encounter.

    ``session_id`` and ``provider_id`` are here because this route touches PHI
    and therefore audits: an ``audit_log`` row needs to name the actor and the
    session, and the caller (TASK-021) has both for the encounter it is watching.
    They are not used to authenticate anything — session JWT validation belongs
    to the WebSocket endpoints TASK-006 issues tokens for, and inventing a
    second auth mechanism here is what Known Constraints #8 rules out.

    ``clinical_context`` is free-form on purpose. It carries whatever the
    transcript scan extracted around a procedure keyword, and pinning a schema
    to it now would freeze a shape TASK-021 has not settled yet. It is typed as
    a mapping rather than a string so a structured context stays structured.
    """

    model_config = ConfigDict(extra="forbid")

    procedure: str = Field(
        min_length=1,
        max_length=200,
        description="The procedure as the clinician described it, e.g. 'knee MRI'.",
    )
    cpt_code: CptCode = Field(
        min_length=1,
        max_length=10,
        description="CPT or HCPCS code for the procedure. Lowercase input is uppercased.",
    )
    payer: PayerName = Field(
        min_length=1,
        max_length=200,
        description=(
            "Issuing payer, in any spelling — including a FHIR Coverage display name "
            "such as 'Medicare Part B'. Resolved to the same slug ingestion used."
        ),
    )
    plan_type: str = Field(
        min_length=1,
        max_length=100,
        description="Plan type, e.g. 'PPO'. Part of the cache key, so it is required here.",
    )
    state: RequiredStateCode = Field(
        pattern=r"^[A-Z]{2}$",
        description="Two-letter state code for the encounter. Lowercase input is uppercased.",
    )
    clinical_context: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "What this encounter has documented so far. Compared against the payer's "
            "criteria on every call and never cached."
        ),
    )
    session_id: uuid.UUID = Field(description="The encounter session, for the audit row.")
    provider_id: uuid.UUID = Field(description="The acting provider, for the audit row.")


class PolicyQueryData(BaseModel):
    """``data`` payload returned by ``POST /policies/query``.

    The first four fields describe the payer's policy and are the half that is
    cached; the middle three describe this encounter's documentation and are
    recomputed on every call.

    ``source`` reports which tier answered, and there is exactly one thing a
    caller may branch on: whether it is ``fallback``. That is not a
    which-cache-did-it-come-from detail but the difference between an answer and
    the absence of one — a fallback means the payer's rules could not be
    established at all, so ``auth_criteria`` is empty because nothing is known
    rather than because nothing is required. TASK-040's emitter needs it to
    withhold the haptic escalation on an answer the system could not verify,
    and it is surfaced here rather than inferred downstream because
    "high risk with no criteria" is a guess at this field, not a reading of it.

    Behaving differently on ``cache`` versus ``rag`` versus ``crd`` is still
    reading something into the response that is not there: those distinguish how
    an established answer was reached, and an established answer is an
    established answer. They are here for the operational trace and for
    recording a nudge's provenance, not as a branch condition.
    """

    requires_auth: bool = Field(
        description="Whether the payer requires prior authorization for this procedure.",
    )
    auth_criteria: list[str] = Field(
        description="What the payer requires to be documented before it will authorize.",
    )
    missing_criteria: list[str] = Field(
        description="Criteria this encounter has not yet documented.",
    )
    denial_risk: Literal["low", "medium", "high"] = Field(
        description="How likely a claim is to be denied given what is documented so far.",
    )
    nudge_message: str | None = Field(
        description=(
            "The message to put in front of the provider, mid-encounter, or null "
            "when there is nothing worth interrupting the consultation for. This "
            "is the nudge trigger (TASK-040): a caller raises a nudge if and only "
            "if this is non-null, and never re-derives that from missing_criteria "
            "or denial_risk, which is how the two came to disagree."
        ),
    )
    step_therapy_required: bool = Field(
        description="Whether the plan requires a first-line therapy to be tried first.",
    )
    step_therapy_details: str | None = Field(
        description="What the step therapy requirement is, when there is one.",
    )
    policy_source: str | None = Field(
        default=None,
        description=(
            "Which indexed policy documents the criteria were read from, "
            "comma-separated, for the nudge record a reviewer checks the "
            "criteria against. Null on a fallback, where no policy text was "
            "read. Derived from retrieval, never from the model's answer."
        ),
    )
    source: RulesSource = Field(
        description=(
            "Which tier established the payer's rules. Branch only on "
            "'fallback', which means they could not be established at all and "
            "the empty auth_criteria therefore means 'unknown', not 'none'. The "
            "rest is provenance for logs and nudge records."
        ),
    )
