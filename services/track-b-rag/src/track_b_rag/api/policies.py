"""``POST /policies/ingest`` — the way policy documents enter the vector store.

**Access control is network isolation, and nothing else.** This endpoint is
reachable only from inside the cluster: its callers are the policy scraper
(TASK-013) and ``scripts/seed-policies.py`` (TASK-014), never a browser and
never an app. It carries no authentication of its own, deliberately — the
session JWT TASK-006 issues is scoped to a clinical encounter and means nothing
to a nightly CronJob, and inventing a second, parallel auth mechanism inside an
ingestion task is exactly what Known Constraints #8 rules out. Service-to-service
authentication is its own future task; until it exists, do not expose this route
through an ingress.

No ``audit_log()`` call either. Insurance policies are public payer publications
with no patient linkage, and Known Constraints #6 makes the audit write
conditional on touching PHI so that every row in ``audit_log`` stays a PHI
access. The ingest is logged at INFO from :mod:`track_b_rag.ingestion` instead.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, status
from qdrant_client import QdrantClient
from sqlalchemy.ext.asyncio import AsyncSession

from api_envelope import ApiHTTPException, ApiResponse, error_responses
from track_b_rag.api.dependencies import get_db_session, get_qdrant
from track_b_rag.api.schemas import IngestPolicyData, IngestPolicyRequest
from track_b_rag.config import get_settings
from track_b_rag.ingestion import EmptyDocumentError, PolicyMetadata, ingest_policy
from track_b_rag.pdf import PdfParseError

router = APIRouter(prefix="/policies", tags=["policies"])

ERROR_CODE_INVALID_PDF = "invalid_pdf"
ERROR_CODE_EMPTY_DOCUMENT = "empty_document"

#: What these statuses mean on this route specifically. api_envelope's generic
#: 400 wording — "the request could not be processed as sent" — would not tell a
#: scraper author whether to fix the fetch or the document.
POLICY_ERROR_DESCRIPTIONS = {
    status.HTTP_400_BAD_REQUEST: (
        "The upload is not a readable PDF, or holds no extractable text to index."
    ),
}


@router.post(
    "/ingest",
    response_model=ApiResponse[IngestPolicyData],
    summary="Ingest a policy document",
    response_description="What the ingest did, and how many chunks it wrote.",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        descriptions=POLICY_ERROR_DESCRIPTIONS,
    ),
)
async def ingest(
    body: Annotated[IngestPolicyRequest, File()],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    client: Annotated[QdrantClient, Depends(get_qdrant)],
) -> ApiResponse[IngestPolicyData]:
    """Index a payer policy PDF, skipping the work when the document is unchanged.

    Chunks the document, embeds it, writes the vectors to Qdrant and records the
    document in `insurance_policies`. Dedup is keyed on the SHA-256 digest of the
    uploaded bytes: a new `policy_id` is `created`, a matching digest is
    `unchanged` and does no work, and a changed digest is `updated` — which
    removes the document's existing chunks before writing the new ones, so a
    superseded revision cannot linger in retrieval.

    Returns 200 in all three cases; the `status` field says which happened.
    Answers 400 when the upload is not a readable PDF or yields no text.

    Internal service-to-service endpoint. Not for frontend use — see the module
    docstring on why it carries no authentication of its own.

    Annotated `File()` rather than `Form()`: both accept the multipart body, but
    only `File()` makes the published spec declare `multipart/form-data`.
    """
    pdf_bytes = await body.file.read()

    try:
        result = await ingest_policy(
            session=session,
            client=client,
            collection=get_settings().qdrant_collection,
            pdf_bytes=pdf_bytes,
            metadata=PolicyMetadata(
                policy_id=body.policy_id,
                payer=body.payer,
                plan_type=body.plan_type,
                state=body.state,
                source_url=body.source_url,
                effective_date=body.effective_date,
            ),
        )
    except PdfParseError as exc:
        raise ApiHTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code=ERROR_CODE_INVALID_PDF,
            message=str(exc),
        ) from exc
    except EmptyDocumentError as exc:
        raise ApiHTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code=ERROR_CODE_EMPTY_DOCUMENT,
            message=str(exc),
        ) from exc

    return ApiResponse[IngestPolicyData](
        data=IngestPolicyData(
            policy_id=result.policy_id,
            status=result.status,
            content_hash=result.content_hash,
            chunks_indexed=result.chunks_indexed,
            collection=result.collection,
        )
    )
