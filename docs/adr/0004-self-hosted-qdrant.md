# ADR-0004: Qdrant, self-hosted, is the vector store

**Status:** Accepted · **Task:** TASK-010

## Context

The RAG path needs a vector store for chunked insurance policy text. Pinecone
and Weaviate Cloud are the managed options; Qdrant, Weaviate and pgvector are
the self-hostable ones.

The indexed content is *not* PHI — insurance policy documents are public payer
publications with no patient linkage. So the HIPAA argument does not strictly
require self-hosting, and this decision has to stand on something else.

## Decision

**Qdrant, self-hosted**: a container in `docker-compose.yml` locally and on EKS
in deployment. Not Pinecone, not Weaviate, not a managed tier.

Embeddings are produced locally by `sentence-transformers` with
`BAAI/bge-large-en-v1.5` (1024 dimensions), so no text leaves the cluster to be
vectorised either.

## Consequences

- **Defence in depth.** The content is not PHI *today*. A later task that puts
  patient-derived text near a vector search would otherwise be one careless
  change away from sending it to a third party, with no infrastructure boundary
  in the way. Self-hosting means the boundary exists before anyone needs it.
- The RAG path has no external dependency that can rate-limit, deprecate an
  index format, or go down independently of us.
- The cost is operational: capacity, upgrades and backups are ours. For a corpus
  measured in tens of documents this is nearly free, and the decision is worth
  revisiting only if the corpus grows by orders of magnitude.
- Local embedding costs ~1.3 GB of model weights and seconds of load time, so
  the embedder is a lazily-created singleton and CI caches the weights.

## References

- `services/track-b-rag/src/track_b_rag/vector_store.py`, `embeddings.py`
- `CLAUDE.md` -> Key Architectural Constraints
