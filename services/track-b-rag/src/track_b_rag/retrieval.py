"""Retrieving the policy passages a query is answered from.

One Qdrant search, filtered to the payer whose rules are being asked about and
to policies that apply in the patient's state. The filter is the reason
TASK-011 created keyword payload indexes on ``payer`` and ``state``: without
them Qdrant still filters, but by scanning, and the collection grows with every
nightly scrape.

The state half of the filter is not a plain equality check. A policy ingested
with no state applies nationally — CMS national coverage determinations are the
obvious case — and an equality filter would hide every one of them from a query
that named a state. Expressed as Qdrant's ``should``, which alongside a ``must``
means "and at least one of these", the filter reads: this payer, and either this
state or no state at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsNullCondition,
    MatchValue,
    PayloadField,
)

from track_b_rag import embeddings

logger = logging.getLogger(__name__)

#: Top 8, per TASK-012. Enough passages that a criteria list spread across a
#: policy's sections survives chunking, few enough that the prompt stays inside
#: a sensible cost per cache miss.
TOP_K: Final = 8


@dataclass(frozen=True)
class RetrievedChunk:
    """One policy passage returned by the vector search."""

    text: str
    policy_id: str
    score: float


def policy_query_filter(*, payer: str, state: str) -> Filter:
    """Return the filter restricting a search to one payer and one state.

    National policies — those ingested with no state — match every state, so
    they are included alongside the state-specific ones rather than filtered
    out. See the module docstring.
    """
    return Filter(
        must=[FieldCondition(key="payer", match=MatchValue(value=payer))],
        should=[
            FieldCondition(key="state", match=MatchValue(value=state)),
            IsNullCondition(is_null=PayloadField(key="state")),
        ],
    )


def build_query_text(*, procedure: str, cpt_code: str, payer: str, plan_type: str) -> str:
    """Return the text embedded and matched against the indexed policy chunks.

    The CPT code leads because it is what the answer is really about: it
    identifies the procedure unambiguously, where `procedure` is whatever the
    transcript called it. Both are included because a bare code embeds poorly —
    the indexed policy text says "magnetic resonance imaging of the lumbar
    spine" far more often than it says "72148".
    """
    return (
        f"Prior authorization requirements for CPT {cpt_code} ({procedure}) "
        f"under a {payer} {plan_type} plan."
    )


def search_policies(
    client: QdrantClient,
    *,
    collection: str,
    query_vector: list[float],
    payer: str,
    state: str,
    limit: int = TOP_K,
) -> list[RetrievedChunk]:
    """Return the highest-scoring policy passages for an already-embedded query.

    Takes the vector rather than the text so the blocking embedding call and the
    blocking Qdrant call can be sequenced by the caller, which runs both in a
    worker thread.
    """
    response = client.query_points(
        collection_name=collection,
        query=query_vector,
        query_filter=policy_query_filter(payer=payer, state=state),
        limit=limit,
        with_payload=True,
    )

    chunks: list[RetrievedChunk] = []
    for point in response.points:
        payload = point.payload or {}
        text = payload.get("text")
        if not text:
            # A point with no text is not usable as context and would only
            # dilute the prompt. It means an ingest wrote a payload this
            # service does not recognise, which is worth a log line.
            logger.warning("Skipping Qdrant point %s: payload carries no text", point.id)
            continue
        chunks.append(
            RetrievedChunk(
                text=str(text),
                policy_id=str(payload.get("policy_id", "")),
                score=float(point.score or 0.0),
            )
        )
    return chunks


def retrieve(
    client: QdrantClient,
    *,
    collection: str,
    procedure: str,
    cpt_code: str,
    payer: str,
    plan_type: str,
    state: str,
    limit: int = TOP_K,
) -> list[RetrievedChunk]:
    """Embed the query and return the policy passages that match it.

    Blocking: both the embedding and the Qdrant call are synchronous, so callers
    on the event loop run this in a worker thread.
    """
    vector = embeddings.embed_query(
        build_query_text(
            procedure=procedure,
            cpt_code=cpt_code,
            payer=payer,
            plan_type=plan_type,
        )
    )
    return search_policies(
        client,
        collection=collection,
        query_vector=vector,
        payer=payer,
        state=state,
        limit=limit,
    )
