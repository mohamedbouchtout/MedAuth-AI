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
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


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

#: The query endpoint takes JSON rather than a form, so an omitted field is
#: absent rather than empty and only the case normalisation applies. It matches
#: what ingestion stores, which is what makes the Qdrant filter and the cache key
#: agree about which payer and which state a request means.
RequiredStateCode = Annotated[str, BeforeValidator(_upper)]
CptCode = Annotated[str, BeforeValidator(_upper)]


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
    payer: str = Field(
        min_length=1,
        max_length=200,
        description="Issuing payer, e.g. 'Aetna' or 'CMS'.",
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
    file: UploadFile = Field(description="The policy document, as a PDF.")


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
    payer: str = Field(
        min_length=1,
        max_length=200,
        description="Issuing payer, spelled as the payer's policies were ingested.",
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
    recomputed on every call. The response does not say which half came from
    where — a caller that behaved differently on a cache hit would be reading
    something into it that is not there.
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
    nudge_message: str = Field(
        description="The message to put in front of the provider, mid-encounter.",
    )
    step_therapy_required: bool = Field(
        description="Whether the plan requires a first-line therapy to be tried first.",
    )
    step_therapy_details: str | None = Field(
        description="What the step therapy requirement is, when there is one.",
    )
