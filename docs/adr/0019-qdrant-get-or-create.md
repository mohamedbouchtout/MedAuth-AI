# ADR-0019: Qdrant collections are get-or-create, never recreated

**Status:** Accepted · **Task:** TASK-010

## Context

`qdrant_client`'s `recreate_collection()` is the method whose name reads like
"make sure this collection exists". It does not. It **drops** the collection and
builds an empty one.

Called from service startup, it wipes every indexed insurance policy on each
restart, rollout and pod reschedule. The failure is silent: an empty collection
is a valid collection, and retrieval simply starts returning nothing — which,
under ADR-0015, looks like a payer we hold no policy for.

## Decision

Startup uses a get-or-create pattern:

```python
def ensure_collection(client: QdrantClient, name: str, vector_size: int):
    try:
        client.get_collection(name)
    except UnexpectedResponse:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
```

`recreate_collection` is acceptable **only** in a one-off dev reset script that
a human runs deliberately. Never in application startup code.

`ensure_payload_indexes()` is idempotent in the same way, creating keyword
payload indexes on `policy_id`, `payer` and `state` only where they are absent.
Those indexes are what keep the retrieval filter from degrading to a scan as the
collection grows with every nightly scrape.

## Consequences

- The service starts against an existing collection without touching it.
- Nothing connects at import time, so the service boots and its health endpoint
  reports "error" while Qdrant is still coming up, rather than failing to start.
- `tests/integration/test_vector_store.py` is the regression test: it indexes,
  restarts, and asserts the points survived.

## References

- `services/track-b-rag/src/track_b_rag/vector_store.py`
- `CLAUDE.md` -> Qdrant Initialization — Must Be Idempotent
