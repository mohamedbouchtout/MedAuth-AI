# System Architecture

MedAuth AI listens to a physician-patient encounter in real time, produces the
clinical documentation, and — the part nothing else on the market does — queries
insurance payer policies **during** the visit, firing an alert the moment a
procedure is ordered without the prior-authorization criteria the payer requires.

The physician finds out before the patient leaves the room, not three weeks later
in a denial letter.

## The moat, stated precisely

Real-time RAG against insurance policy databases *during* the clinical encounter.
Everything else in this document exists to make that latency-bounded, cheap
enough to run per encounter, and safe enough to put in front of a clinician.

Three constraints follow from that sentence and shape almost every decision below:

1. **It has to answer inside a live conversation.** Seconds, not minutes.
2. **It has to be cheap per encounter.** A vector search plus a Sonnet call for
   every procedure mention, times every visit in a practice, is the dominant
   cost of the product. See [ADR-0014](../adr/0014-two-stage-policy-answer.md).
3. **It must never imply an unauthorized order is clear.** Silence reads as
   approval to a busy clinician. See
   [ADR-0015](../adr/0015-fail-toward-authorization-required.md).

## Runtime shape

```
                         apps/web  ·  apps/mobile
                      (React 19)      (Expo SDK 57)
                              |
             session JWT      |      250ms frames, 16kHz mono int16 PCM
        POST /sessions/start  |      WebSocket /ws/audio/{session_id}
                    +---------+---------+
                    v                   v
          track-a-clinical        audio-ingestion --> AWS Transcribe Medical
        (owns encounters,          in-memory only,      (streaming, HIPAA-eligible)
         mints the JWT)            never a file
                    |                   |
                    |                   v
                    |        Redis  transcription:{session_id}
                    |                   |
              +-----+-------------------+-------------------+
              v                                             v
     track-a-clinical                                 track-b-rag
     accumulates transcript,                  scans for procedure keywords,
     SOAP + ICD-10 via Sonnet                 resolves a CPT code, queries
     on Bedrock            [TASK-030]         payer policy
              |                                             |
              |                       +---------------------+------------------+
              |                       v                                        v
              |             Qdrant + Sonnet (RAG)                 payer's Da Vinci CRD
              |             criteria, step therapy                requires_auth
              |             cached 24h                            never cached
              |                       +---------------------+------------------+
              |                                             v
              |                                Redis  nudges:{session_id}
              |                                             v
              v                                       nudge-service
     fhir-integration  <---- prior-auth ---->    WebSocket relay  [TASK-041]
     SOAP write-back        bundle assembly              v
     to the EHR   [P5]      [TASK-060]        live alert in the exam room
```

**Two tracks run off one audio stream.** Track A produces the documentation;
Track B produces the prior-auth intelligence. They subscribe to the same Redis
channel, never see each other, and share nothing but the transcript.

## The path a nudge actually takes

1. A provider taps "start visit". `POST /sessions/start` creates the `encounters`
   row, mints a 15-minute session JWT, and announces the session on
   `sessions:started` — **before** returning the token, so a consumer is
   listening before a client can open its socket
   ([ADR-0028](../adr/0028-per-session-subscription.md)).
2. The client opens `WebSocket /ws/audio/{session_id}`, carrying the JWT in a
   header or a subprotocol ([ADR-0013](../adr/0013-two-websocket-token-carriers.md)).
   Validation happens **before** the handshake completes.
3. Audio streams as 250ms 16kHz mono int16 PCM frames. `audio-ingestion` buffers
   in memory, pushes fixed-size chunks to Transcribe Medical, and publishes only
   **stabilized** segments ([ADR-0027](../adr/0027-publish-stabilized-segments-only.md)).
4. `track-b-rag` scans each segment for procedure keywords
   ([ADR-0030](../adr/0030-keyword-detection-not-a-model.md)) and resolves the
   spoken phrase to a CPT code, refusing where it cannot
   ([ADR-0031](../adr/0031-cpt-resolver-refuses-rather-than-guesses.md)).
5. A first mention is claimed atomically in Redis so one order raises one nudge
   ([ADR-0029](../adr/0029-one-nudge-per-procedure-per-encounter.md)).
6. `POST /policies/query` runs Stage 1 (payer rules, cached, plus a concurrent
   CRD call) and Stage 2 (this encounter's gaps, never cached) —
   [ADR-0014](../adr/0014-two-stage-policy-answer.md),
   [ADR-0016](../adr/0016-two-tier-crd-and-rag.md).
7. The nudge is published to `nudges:{session_id}` and relayed to the client.

Steps 6 and 7 are where the product lives. See
[design/rag-policy-lookup.md](../design/rag-policy-lookup.md).

## Where the state is

| Store | Holds | Notes |
|---|---|---|
| **PostgreSQL** | Encounters, notes, nudges, prior-auth requests, policy metadata, audit log | Alembic migrations; soft deletes; server-side UUIDs |
| **Redis** | Pub/sub channels, the payer-policy cache, per-encounter dedup sets, SMART launch state | No durability by design — [ADR-0003](../adr/0003-redis-pubsub-not-kafka.md) |
| **Qdrant** | Chunked payer policy text, 1024-dim vectors | Self-hosted — [ADR-0004](../adr/0004-self-hosted-qdrant.md) |
| **Nowhere** | Encounter audio | [ADR-0005](../adr/0005-audio-never-persists.md) |

Full detail in [data-model.md](./data-model.md).

## Layering rules

- **Every service is Python 3.12 in one uv workspace**
  ([ADR-0002](../adr/0002-one-python-uv-workspace.md)). The npm workspaces cover
  the two frontends and the TypeScript packages only.
- **REST between services**, with the `{"data": ..., "error": null}` envelope
  from `packages/api-envelope`
  ([ADR-0010](../adr/0010-single-response-envelope-package.md)), cursor
  pagination, ISO 8601 UTC timestamps, and OpenAPI specs in `docs/api/`.
- **Shared code goes in `packages/`** the moment a second consumer appears, not
  later. That trigger produced `api-envelope`, `payer-vocab` and `audio-wire`.
- **One definition of anything cross-cutting**: the SQLAlchemy models
  ([ADR-0009](../adr/0009-one-sqlalchemy-model-definition.md)), the response
  envelope, the payer vocabulary
  ([ADR-0022](../adr/0022-canonical-payer-slugs.md)), the audio wire format
  ([ADR-0036](../adr/0036-audio-wire-format-package.md)), and the
  backing-service versions
  ([ADR-0037](../adr/0037-compose-is-the-only-version-pin.md)).
- **EHR-specific behaviour lives in an adapter subclass**, never in a route
  handler or another service. The SMART launch `iss` selects the adapter at
  runtime; EHR type is never hardcoded.

## Failure philosophy

The system is asymmetric about which way it fails, deliberately and everywhere:

| Situation | Behaviour | Why |
|---|---|---|
| Policy rules unresolvable | 200 with "authorization required, confirm manually" | Silence reads as approval |
| Payer returns no determination | RAG answers alone | Silence is not a "no" |
| Redis dedup unreachable | Fail **open** — allow a duplicate nudge | Never suppress a first warning |
| Redis cache unreachable | Treat as a miss, pay full cost | A cache is not a correctness dependency |
| Cached entry unparseable | Discard, recompute | A cache that can poison a request is worse than none |
| Spoken phrase ambiguous | Do not query at all | A wrong CPT code poisons a shared cache key |
| Criterion match uncertain | Report it as missing | A silent documentation gap is the costly error |
| Session announcement fails | **503**, session unusable | An unwatched encounter looks like a clean one |
| Sample rate mismatch | Never open the socket | Transcribe hangs rather than errors |

## What is built

Phases 0-2 are substantially complete: the shared packages, the database schema,
session lifecycle, the entire RAG pipeline including the CRD tier, the audio
WebSocket, the transcript fan-out, and both clients' capture layers.

Phases 3-10 — SOAP generation, the nudge relay, FHIR integration, prior auth
bundle assembly, and the provider UI — are designed and unbuilt. See
[service-catalog.md](./service-catalog.md) for the per-service position and
`TASKS.md` for the authoritative status.
