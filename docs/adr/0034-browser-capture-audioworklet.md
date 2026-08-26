# ADR-0034: Browser capture uses AudioWorklet, not MediaRecorder

**Status:** Accepted · **Task:** TASK-023

## Context

`MediaRecorder` is the obvious browser recording API and is what earlier drafts
of `CLAUDE.md` named. Three measured properties rule it out for this pipeline:

- **It cannot emit raw PCM at all.** It produces container-framed encoded audio
  (WebM/Opus, MP4/AAC). Transcribe Medical streaming is configured for
  `pcm` at 16kHz.
- **It offers no sample-rate control.** The device rate — commonly 48kHz — is
  what you get.
- **Its `timeslice` chunks are not independently decodable.** Only the first
  carries the container header, so a chunk cannot be forwarded as a standalone
  frame.

TASK-020 separately established that Transcribe answers a sample rate
disagreeing with the audio **by hanging rather than erroring**. A mismatch is
therefore not a degraded stream — it is a stream that never produces a
transcript and never says why.

## Decision

The capture graph is
`getUserMedia` -> `AudioContext({ sampleRate: 16000 })` -> `AudioWorkletNode`.

The `AudioContext` is the only resampler involved: it is what turns a device's
48kHz into the 16kHz Transcribe is configured for. Float-to-int16 conversion and
250ms framing live in `packages/audio-wire` (ADR-0036).

**The order of operations is the point, not an implementation detail:**

```
permission -> open the context -> compare the rate it reports
           -> compare the first worklet message -> only then open the WebSocket
```

A mismatch therefore means no socket was ever opened and **not one byte of audio
left the browser**. The comparison happens twice on purpose: the context's rate
does not depend on audio ever arriving, and the first message is the audio that
would really be sent.

The worklet processor itself is **deliberately the smallest thing that can
work** — it copies each render quantum's input channel and posts it to the main
thread, converting nothing and deciding nothing. `AudioWorkletProcessor` does
not exist in jsdom, so anything implemented there is code the tests cannot
reach. It is plain JavaScript loaded by URL rather than imported, because
`audioWorklet.addModule()` fetches a real module into a separate global scope:
it is an asset the bundler copies, not part of the app graph.

## Consequences

- The measurements are recorded in TASK-023 so `MediaRecorder` does not get
  proposed again as a simplification.
- The node has an output only so the graph can reach the destination and be
  guaranteed a `process()` call in every engine; what travels down it is
  silence, with a zero gain in the way as well.
- Audio lives only in the framer's buffer and is dropped on every exit path
  (ADR-0005). The session JWT rides in the subprotocol list (ADR-0013), never in
  the URL and never in a log line.

## References

- `apps/web/src/hooks/useAudioCapture.ts`, `apps/web/src/audio/pcm-capture-processor.js`
