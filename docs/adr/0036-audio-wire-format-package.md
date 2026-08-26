# ADR-0036: The audio wire format is defined once, in a shared package

**Status:** Accepted · **Task:** TASK-022, TASK-023

## Context

Two clients — `apps/web` and `apps/mobile` — stream to the same
`audio-ingestion` endpoint and must agree **byte for byte** on sample rate,
channel count, encoding and frame size.

`audio-ingestion` forwards whatever it receives straight to Transcribe Medical,
which is configured from `TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ` and
`TRANSCRIBE_MEDICAL_MEDIA_ENCODING`. A mismatch is not a degraded stream: TASK-020
established that Transcribe answers a disagreeing sample rate **by hanging rather
than erroring**, so a wrong constant produces a stream that never yields a
transcript and never says why.

Two hand-maintained copies of a wire contract drift, for the same reason
`packages/api-envelope` exists.

## Decision

`packages/audio-wire` is the single definition, imported by both apps and by
their tests:

| Export | Value | Note |
|---|---|---|
| `SAMPLE_RATE_HZ` | 16000 | Must equal `TRANSCRIBE_MEDICAL_SAMPLE_RATE_HZ` |
| `CHANNELS` | 1 | Transcribe Medical streaming takes a single channel |
| `ENCODING` | `'int16'` | Doubles as `expo-audio`'s `encoding` option value |
| `CHUNK_DURATION_MS` | 250 | |
| `CHUNK_BYTES` | 8000 | `16000 x 2 x 1 x 0.25` |

It also holds the two pieces of logic both clients need at that boundary:

- **`PcmFramer`** re-chunks variable-sized capture buffers into fixed 250ms
  frames. Neither platform hands audio over in this size — `onBuffer` delivers
  whatever the native layer picked, an `AudioWorkletProcessor` delivers
  128-sample render quanta — so both re-chunk to the same boundary. It is a
  plain class with no React and no I/O, because the chunking boundary is the
  part most likely to be wrong in a way tests can catch.
- **`floatToInt16LE`** converts normalised float samples. Only the browser calls
  it today; it lives here because what it produces *is* the wire format.

Two properties of that conversion are the whole point of the function:

- **Little-endian is written explicitly, not inherited.** `DataView.setInt16`
  takes byte order as an argument, so the output is identical on a big-endian
  host. The wrong answer here would be inaudible noise rather than a crash.
- **Out-of-range input is clamped, not wrapped.** Web Audio does not promise
  samples stay inside [-1, 1]; gain, or simply a loud room, pushes past it.
  Multiplying and truncating without a clamp turns a peak into a sample of the
  opposite sign — an audible click on a transcript's loudest moment, usually the
  part someone wants transcribed most.

## Consequences

- The package ships **source** that both apps compile into themselves, not a
  built artefact. A change here therefore sets the `web` and `mobile` CI filters
  as well as its own.
- It runs `tsc --noEmit` and Vitest rather than joining the Python matrix, and
  carries the same 80% coverage gate as every other package.

## References

- `packages/audio-wire/src/format.ts`, `framing.ts`, `pcm.ts`
