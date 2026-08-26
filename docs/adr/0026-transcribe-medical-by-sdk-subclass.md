# ADR-0026: Transcribe Medical is reached by subclassing the AWS streaming SDK

**Status:** Accepted · **Task:** TASK-020

## Context

`amazon-transcribe` — the official AWS Transcribe Streaming SDK for Python,
pinned at 0.6.4 — implements `StartStreamTranscription` and nothing else. There
is no `start_medical_stream_transcription` on its client, no `specialty` or
`type` anywhere in the package, and its serializer writes the request URI
`/stream-transcription` as a literal.

`boto3` is not an alternative: it has no streaming transcription API at all,
only the batch job API. That is why this service depends on the streaming SDK in
the first place.

The difference between the two operations is not cosmetic. The **medical** model
is what recognises drug names, dosages, anatomy and procedure terms — exactly
the vocabulary the keyword scan and SOAP generation consume downstream.
Transcribing a clinical encounter with the general model and calling it the same
thing would quietly degrade every consumer.

## Decision

Reach the medical operation by **subclassing the SDK's serializer and client**.
The wire difference is small and entirely in the request: a different URI
(`/medical-stream-transcription`) and two extra headers
(`x-amzn-transcribe-specialty`, `x-amzn-transcribe-type`).

The response side needs nothing. The medical stream emits the same
`:event-type: TranscriptEvent` framing, and the SDK's parser reads every field
with a tolerant `.get()`, so a `MedicalResult` — no `ChannelId`, an extra
`Entities` — parses through the existing path unchanged.

## Consequences

This is a patch of another project's internals, not configuration of it.
`TranscribeStreamingSerializer.serialize_start_stream_transcription_request` and
`TranscribeStreamingClient._serializer` are not published extension points, and
AWS has shipped no release since 0.6.4. What we accept in exchange, deliberately:

- **The dependency is pinned to `==0.6.4`**, not given a floor. A Dependabot
  bump here is a change to review, not a routine one.
- **`tests/unit/test_transcribe_medical.py` asserts the serialized request** —
  the URI and both headers — rather than trusting the subclass to have taken
  effect. If a future version restructures serialization so the override stops
  being called, that test fails rather than the service silently transcribing a
  clinical encounter with the general model.
- **If AWS ever adds medical support upstream, delete both classes and call the
  real method.** Nothing outside the `TranscriptionStream` protocol depends on
  either of them.

That protocol is the other half of the arrangement: the WebSocket route depends
on it and never on AWS, which is what lets the unit suite drive a full
connection — handshake, audio frames, published segments, teardown — against an
injected fake in milliseconds. It is a seam for testability, not an abstraction
layer: there is one real implementation and no intention of a second, so it
stays as narrow as the route's actual needs.

## References

- `services/audio-ingestion/src/transcribe_medical.py`, `transcription.py`
