"""SQLAlchemy models for the schema track-a-clinical migrates.

This is the single definition of these five tables for the whole monorepo. A
service that writes one of them imports the class from here rather than mapping
its own — ``track-b-rag`` writes ``clinical_nudges``, ``prior-auth`` writes
``prior_auth_requests``, and both read ``encounters``::

    from track_a_clinical.models import ClinicalNudge, Encounter

Importing this package registers every model on ``Base.metadata``, which is what
lets Alembic autogenerate see the whole schema and what lets the string-based
``relationship()`` targets resolve. Import the package, not the individual
modules.

``audit_log`` is deliberately absent — it belongs to packages/hipaa-logger, which
owns its own migration and applies first.
"""

from __future__ import annotations

from track_a_clinical.models.base import Base, JsonObject
from track_a_clinical.models.clinical_note import ClinicalNote
from track_a_clinical.models.clinical_nudge import (
    DENIAL_RISK_HIGH,
    DENIAL_RISK_LOW,
    DENIAL_RISK_MEDIUM,
    ClinicalNudge,
)
from track_a_clinical.models.encounter import (
    ENCOUNTER_STATUS_ACTIVE,
    ENCOUNTER_STATUS_COMPLETED,
    Encounter,
)
from track_a_clinical.models.extracted_code import (
    SOURCE_COMPREHEND_MEDICAL,
    SOURCE_LLM_EXTRACTION,
    SOURCE_PROVIDER_ACCEPTED,
    CodeValidation,
    ExtractedCode,
    dump_codes,
    load_codes,
    matching_key,
)
from track_a_clinical.models.insurance_policy import (
    DEFAULT_QDRANT_COLLECTION,
    InsurancePolicy,
)
from track_a_clinical.models.prior_auth_request import (
    PRIOR_AUTH_STATUS_APPROVED,
    PRIOR_AUTH_STATUS_DENIED,
    PRIOR_AUTH_STATUS_PENDING,
    PRIOR_AUTH_STATUS_SUBMITTED,
    PriorAuthRequest,
    SubmissionMethod,
)

__all__ = [
    "DEFAULT_QDRANT_COLLECTION",
    "DENIAL_RISK_HIGH",
    "DENIAL_RISK_LOW",
    "DENIAL_RISK_MEDIUM",
    "ENCOUNTER_STATUS_ACTIVE",
    "ENCOUNTER_STATUS_COMPLETED",
    "PRIOR_AUTH_STATUS_APPROVED",
    "PRIOR_AUTH_STATUS_DENIED",
    "PRIOR_AUTH_STATUS_PENDING",
    "PRIOR_AUTH_STATUS_SUBMITTED",
    "SOURCE_COMPREHEND_MEDICAL",
    "SOURCE_LLM_EXTRACTION",
    "SOURCE_PROVIDER_ACCEPTED",
    "Base",
    "ClinicalNote",
    "ClinicalNudge",
    "CodeValidation",
    "Encounter",
    "ExtractedCode",
    "InsurancePolicy",
    "JsonObject",
    "PriorAuthRequest",
    "SubmissionMethod",
    "dump_codes",
    "load_codes",
    "matching_key",
]
