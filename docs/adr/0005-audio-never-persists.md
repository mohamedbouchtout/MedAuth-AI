# ADR-0005: Encounter audio never touches disk

**Status:** Accepted · **Task:** TASK-020, TASK-022, TASK-023

## Context

The system captures continuous audio of physician-patient conversations. That
recording is the most sensitive artefact the platform handles: it carries every
identifier, every incidental disclosure, and everything said in the room that
was never meant for the record. Encryption at rest reduces the risk of a stored
recording; it does not remove the recording.

The transcript is the artefact of value. Once Transcribe Medical has produced
it, the audio has no further purpose in this product.

## Decision

Audio exists only in memory, on every tier, and is discarded as soon as it has
been forwarded.

- **Clients** hold audio in a `PcmFramer` buffer and drop it on every exit path.
  Neither client writes a file, and neither uses a recording API that produces
  one — see ADR-0034 and ADR-0035.
- **audio-ingestion** accumulates client frames in a single in-memory
  `AudioBuffer` and pushes fixed-size chunks to Transcribe. On disconnect the
  remainder is flushed, the stream is ended, and the buffer is explicitly
  cleared.
- Nothing in the path opens a file handle, and no bucket exists to write to.

The buffer is bounded by `MAX_BUFFERED_BYTES`, so a client that streams faster
than the far side drains raises `AudioBufferOverflow` rather than growing until
the process dies.

## Consequences

- **No re-transcription.** A transcription that fails, or that later turns out
  to have been produced with the wrong model, cannot be re-run — the source is
  gone. That is a real product cost and it is accepted.
- No quality-assurance listening, no training corpus, no audio evidence in a
  dispute about what was said.
- The constraint is enforceable by reading one file per tier, because exactly
  one module on each tier holds audio at all. That is deliberate: a reviewer
  should be able to verify the claim rather than take it on trust.

## References

- `services/audio-ingestion/src/audio.py`
- `packages/audio-wire/src/framing.ts`
- `CLAUDE.md` -> Key Architectural Constraints
