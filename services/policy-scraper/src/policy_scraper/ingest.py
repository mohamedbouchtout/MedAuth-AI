"""Handing a document to ``POST /policies/ingest``.

Everything expensive — chunking, embedding, the Qdrant write, the dedup
decision — belongs to that endpoint. This service fetches, filters, resolves
jurisdictions and uploads; it does not reimplement any of the pipeline, which is
TASK-013's explicit instruction and the reason there is one definition of how a
policy gets indexed rather than two that drift.

The upload declares ``text/html``, because that is what CMS publishes and what
the export carries. See ``track_b_rag.documents`` for why the digest is taken
over those bytes rather than over anything this service renders.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from policy_scraper.documents import CMS_PAYER, PolicyDocument

logger = logging.getLogger(__name__)


class IngestFailed(RuntimeError):
    """Raised when the ingest endpoint rejected or failed on a document."""


def _form_fields(document: PolicyDocument) -> dict[str, str | list[str]]:
    """Return the multipart metadata fields for one document.

    ``jurisdiction_states`` is a list value, which httpx renders as repeated
    form fields — a Medicare LCD covers its contractor's whole jurisdiction and
    every state in it is its own field. (A list of pairs would express the same
    thing, but httpx only accepts the mapping form alongside ``files``.)
    """
    fields: dict[str, str | list[str]] = {
        "policy_id": document.policy_id,
        "payer": CMS_PAYER,
        "content_type": "text/html",
        "source_url": document.source_url,
    }
    if document.effective_date is not None:
        fields["effective_date"] = document.effective_date.isoformat()
    if document.states:
        fields["jurisdiction_states"] = document.states
    return fields


async def upload(
    client: httpx.AsyncClient, *, base_url: str, document: PolicyDocument
) -> dict[str, Any]:
    """Upload one document and return the ingest result.

    Args:
        client: An HTTP client for the internal service network.
        base_url: Where track-b-rag is reachable.
        document: The assembled policy.

    Returns:
        The ``data`` payload — ``status``, ``content_hash``, ``chunks_indexed``.

    Raises:
        IngestFailed: The endpoint answered with an error, or with a body this
            service does not recognise. Both are worth failing on: a scrape that
            counted a rejected document as ingested would report success while
            leaving the collection missing a policy.
    """
    response = await client.post(
        f"{base_url}/policies/ingest",
        data=_form_fields(document),
        files={"file": (f"{document.policy_id}.html", document.body, "text/html")},
    )

    if response.status_code != httpx.codes.OK:
        raise IngestFailed(
            f"Ingest of {document.policy_id} failed with HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    body = response.json()
    data = body.get("data")
    if not isinstance(data, dict) or "status" not in data:
        raise IngestFailed(
            f"Ingest of {document.policy_id} returned an unrecognised body: {body!r}"
        )

    logger.info(
        "Ingested %s: %s, %s chunks",
        document.policy_id,
        data["status"],
        data.get("chunks_indexed"),
    )
    return data
