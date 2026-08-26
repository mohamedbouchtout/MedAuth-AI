# ADR-0035: Mobile capture uses `expo-audio`'s `useAudioStream`, not `expo-av`

**Status:** Accepted · **Task:** TASK-022

## Context

Earlier drafts of `CLAUDE.md` and of TASK-022 named `expo-av` for mobile audio
capture. That was written without checking it — the same way the Node.js
`fhir-integration` line was.

`expo-av` **records to a file URI and exposes no PCM callback at all.** It could
satisfy neither the "audio never persists" constraint (ADR-0005) nor the
16kHz-PCM wire format. It was also removed from Expo entirely in SDK 55.

The instinct at that point is to reach for a third-party native module. The
first-party package answers it: `expo-audio`'s `useAudioStream` delivers
real-time PCM buffers to an `onBuffer` callback, never a recorded file. It
landed in SDK 56.

## Decision

Mobile capture uses `expo-audio`'s `useAudioStream`. The Expo SDK pin moves from
51 to **57**. `apps/mobile` is unscaffolded, so adopting a newer SDK costs
nothing.

The order of operations mirrors the web (ADR-0034), for the same reason —
Transcribe hangs rather than errors on a rate mismatch:

```
permission -> start stream -> compare the rate the stream reports
           -> compare the first buffer delivered -> only then open the WebSocket
```

`AudioStream` publishes the rate it *actually* captured at, which may differ from
the one requested if the hardware cannot oblige. That catches a mismatch without
depending on a buffer ever arriving; the first buffer is checked as well because
it is the audio that would really reach Transcribe, and the two disagreeing is
itself a reason not to stream.

Mobile needs no float conversion — `expo-audio` fills an int16 buffer natively
and the bytes are forwarded untouched — but it *does* need an endianness check,
because the native buffer's byte order is the host's.

## Consequences

- Checking the framework's own first-party package before proposing a
  third-party native module is the general lesson here, not a one-off.
- The JWT goes in an `Authorization` header on mobile — React Native's WebSocket
  accepts headers where a browser's does not (ADR-0013).
- Audio lives only in the framer's buffer and is dropped on every exit path.
- `PcmFramer` **copies** each incoming buffer rather than retaining it: on
  mobile `AudioStreamBuffer.data` comes from the native layer and nothing
  promises the same `ArrayBuffer` is not handed back on the next callback.
  Retaining it directly would let a later capture overwrite audio still waiting
  to be sent.

## References

- `apps/mobile/src/hooks/useAudioCapture.ts`
- `packages/audio-wire/src/framing.ts`
