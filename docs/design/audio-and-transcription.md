# Design: Audio Capture and Transcription

**Components:** `apps/web`, `apps/mobile`, `packages/audio-wire`,
`services/audio-ingestion` · **Tasks:** TASK-020, TASK-022, TASK-023

The path from a microphone in an exam room to a transcript segment on the Redis
bus. Everything here is shaped by two facts: **the audio is the most sensitive
artefact the platform touches**, and **AWS Transcribe answers a sample-rate
mismatch by hanging rather than erroring**.

## The wire format

Fixed once, in `packages/audio-wire`, and imported by both clients and their
tests ([ADR-0036](../adr/0036-audio-wire-format-package.md)):

| | |
|---|---|
| Sample rate | **16,000 Hz** — must equal `TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ` |
| Channels | **1** (mono) |
| Encoding | **int16 little-endian** PCM |
| Frame | **250 ms** = **8,000 bytes** (`16000 x 2 x 1 x 0.25`) |

These are not free choices. `audio-ingestion` forwards what it receives straight
to Transcribe Medical, which is configured from `.env.example`. A wrong constant
does not degrade the stream — it produces a stream that never yields a transcript
and never says why.

## Capture

Neither platform hands audio over in 250 ms pieces, so both re-chunk to the same
boundary using the same `PcmFramer`.

### Browser — `apps/web`

```
getUserMedia -> AudioContext({ sampleRate: 16000 }) -> AudioWorkletNode
             -> floatToInt16LE -> PcmFramer -> WebSocket
```

`MediaRecorder` was ruled out on three measured grounds: it cannot emit raw PCM
at all, it offers no sample-rate control, and its `timeslice` chunks are not
independently decodable ([ADR-0034](../adr/0034-browser-capture-audioworklet.md)).

The `AudioContext` is the only resampler involved — it is what turns a device's
common 48 kHz into 16 kHz.

The worklet processor is deliberately minimal: it copies each render quantum's
input channel and posts it to the main thread, converting nothing and deciding
nothing. `AudioWorkletProcessor` does not exist in jsdom, so anything implemented
there is code the tests cannot reach. Conversion and framing live in
`audio-wire` as pure functions with their own suite.

### Mobile — `apps/mobile`

```
expo-audio useAudioStream -> onBuffer -> PcmFramer -> WebSocket
```

`expo-av` was ruled out: it records to a file URI, exposes no PCM callback, and
was removed from Expo in SDK 55. `useAudioStream` landed in SDK 56, which is why
the SDK pin moved from 51 to 57 ([ADR-0035](../adr/0035-mobile-capture-expo-audio.md)).

No float conversion is needed — `expo-audio` fills an int16 buffer natively — but
an endianness check *is*, because the native buffer's byte order is the host's.

### The two-stage format check

Both clients run the same sequence, and the order is the point:

```
permission -> open the stream -> compare the rate it reports
           -> compare the first buffer delivered -> only then open the WebSocket
```

A mismatch therefore means **no socket was ever opened and not one byte of audio
left the device.**

The comparison happens twice on purpose. The stream's reported rate does not
depend on audio ever arriving, so it catches the mismatch early; the first
buffer is checked as well because it is the audio that would really reach
Transcribe, and the two disagreeing is itself a reason not to stream.

### Errors

Nothing in either hook throws. State is a discriminated union carrying a typed
`AudioCaptureError`, because a thrown error inside a React hook is precisely
what an error boundary swallows — leaving a blank panel and no explanation for
why the encounter is not being recorded
([ADR-0039](../adr/0039-typed-result-errors-in-clients.md)).

### `PcmFramer`

A plain class with no React and no I/O, so a test can drive the chunking
boundary directly. Two details matter:

- It **copies** each incoming buffer. On mobile the buffer comes from the native
  layer and nothing promises the same `ArrayBuffer` is not handed back on the
  next callback; retaining it would let a later capture overwrite audio still
  waiting to be sent.
- It is **bounded**. Audio accumulating past `MAX_PENDING_BYTES` because the
  socket never opened raises `PendingAudioOverflow`, whose message carries byte
  counts only — never the audio and nothing derived from it.

## The connection

`WebSocket /ws/audio/{session_id}` on `audio-ingestion`, port 8001.

The session JWT arrives in **either** an `Authorization: Bearer` header or the
`Sec-WebSocket-Protocol` list as `medauth.jwt.<jwt>` alongside a
`medauth.session.v1` version marker. Service-to-service callers and tests use the
header; the browser must use the subprotocol, because the native `WebSocket`
constructor takes a URL and a subprotocol list and nothing else. React Native's
WebSocket does accept headers, so mobile uses the header form
([ADR-0013](../adr/0013-two-websocket-token-carriers.md)).

Validation — signature, expiry, and `session_id` claim matching the URL path —
runs **before the handshake is accepted**, so a refused token never reaches a
state where it can send a frame, and no transcription stream is opened for it.

The accept echoes `medauth.session.v1` and **never the token**: a browser aborts a
connection whose handshake response does not name one of the subprotocols it
offered, so something must be selected, and selecting the token entry would write
the credential into every proxy access log on the path.

## The connection lifecycle

1. Validate the JWT, from either carrier, before the handshake completes.
2. Accept, echoing the version marker if subprotocols were offered, and write the
   access to the audit log.
3. Client frames accumulate in an in-memory `AudioBuffer` and are pushed to
   Transcribe Medical in fixed-size chunks. **Nothing is written to disk at any
   point.**
4. Segments coming back are published to `transcription:{session_id}`.
5. On disconnect: flush the buffer's remainder, end the transcription stream,
   clear the buffer explicitly.

Steps 3 and 4 run concurrently in a task group — audio arrives and results come
back on their own schedules, and serialising them would add the whole
transcription latency to every frame. A failure in either cancels the other, so a
connection never half-survives.

Close codes: **4401** for an unauthorized handshake, **1003** for an unsupported
frame type, **1011** for an internal error. Note that a connection refused before
the handshake completes has no WebSocket frame to carry a code in — the 4401 is
what the application emits and what an ASGI-level test observes; a browser sees a
failed upgrade.

## Why buffer at all

The client sends whatever frame size its capture API produces, and Transcribe
bills a signed event per chunk. Very small frames waste signatures and round
trips; very large ones add latency to a nudge budget measured in seconds. The
buffer decouples the two, accumulating client frames and releasing them at a
fixed threshold (`DEFAULT_FLUSH_THRESHOLD_BYTES` = 8,000).

## Reaching Transcribe *Medical*

The official `amazon-transcribe` SDK (pinned `==0.6.4`) implements
`StartStreamTranscription` and nothing else — no medical operation, no
`specialty` or `type` parameters. `boto3` has no streaming transcription API at
all.

The medical operation is reached by **subclassing the SDK's serializer and
client**. The wire difference is entirely in the request: URI
`/medical-stream-transcription` plus `x-amzn-transcribe-specialty` and
`x-amzn-transcribe-type` headers. The response side needs nothing — the medical
stream uses the same event framing and the SDK's parser reads every field
tolerantly ([ADR-0026](../adr/0026-transcribe-medical-by-sdk-subclass.md)).

This matters because the medical model is what recognises drug names, dosages,
anatomy and procedure terms — exactly the vocabulary the keyword scan and SOAP
generation consume downstream.

The unit test asserts the **serialized request**, not the subclass, so a future
SDK release that restructures serialization fails the build rather than silently
transcribing a clinical encounter with the general model.

### The testability seam

The WebSocket route depends on a `TranscriptionStream` protocol and never on
AWS. That is what lets the unit suite drive a full connection — handshake, audio
frames, published segments, teardown — against an injected fake in milliseconds.

It is a seam for testability, not an abstraction layer: there is exactly one real
implementation and no intention of a second, so it stays as narrow as the route's
actual needs — push audio, read segments, stop.

## Publication

Only **stabilized** segments reach `transcription:{session_id}`. Transcribe emits
a partial result for an utterance repeatedly as it revises it — the same
`result_id`, several times a second — then one final result. Forwarding partials
would multiply bus traffic by an order of magnitude and make the keyword scan
fire the same procedure over and over as one sentence is re-transcribed
([ADR-0027](../adr/0027-publish-stabilized-segments-only.md)).

The payload still carries `is_partial`, so a later task can widen this without
changing the message shape or the consumers' parsing.

**The payload is PHI.** It goes to Redis and nowhere else; the publisher logs
that a segment was published and never what was in it.

## Two independent consumers

`track-a-clinical` accumulates the transcript for SOAP generation (TASK-030) and
`track-b-rag` scans it for procedure keywords (TASK-021). Neither is a shared
component; they subscribe to the same channel and never see each other.
`audio-ingestion` publishes once and Redis fans out.

## What is not built

- **TASK-026** — a mobile capture deadline that fails when no audio arrives.
- **TASK-025** — the mobile session screen. Both hooks exist; neither app has a
  UI that drives one yet.
