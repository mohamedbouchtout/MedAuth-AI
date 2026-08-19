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
from typing import Annotated, Literal

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
