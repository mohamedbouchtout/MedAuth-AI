# ADR-0039: Client errors are typed Result unions, never thrown

**Status:** Accepted · **Task:** TASK-022, TASK-023

## Context

The TypeScript convention in this repository is that errors bubble up as typed
Result objects rather than thrown exceptions. In the audio capture hooks this
stops being stylistic.

`useAudioCapture` can fail in ways the user must be told about specifically:
microphone permission denied, a sample-rate mismatch that means no socket will
open, a pending-audio overflow because the socket never came up, a WebSocket
that closed. Each needs different UI.

**A thrown error inside a React hook is precisely what an error boundary
swallows.** The component unmounts, the user sees a blank panel, and the reason
the encounter is not being recorded is gone.

## Decision

Nothing in the capture hooks throws. State is a **discriminated union** carrying
a typed `AudioCaptureError`, and the caller narrows on it.

The same convention holds across the TypeScript surface: strict mode with no
`any` (use `unknown` and narrow), interfaces for data shapes and type aliases
for unions, and named exports only in shared packages.

## Consequences

- Every failure is visible in the component tree as state, and every one has to
  be handled explicitly — the compiler will not let a case be dropped.
- The overflow case (`PendingAudioOverflow`) carries **byte counts only**, never
  the audio and nothing derived from it.
- `noUncheckedIndexedAccess` is on, which is why the hot conversion loop in
  `pcm.ts` iterates a `Float32Array` by value rather than by index: indexing
  would force a `?? 0` fallback that can never be taken, leaving a permanently
  uncovered branch in the middle of the loop.

## References

- `apps/web/src/hooks/useAudioCapture.ts`, `apps/mobile/src/hooks/useAudioCapture.ts`
- `packages/audio-wire/src/errors.ts`
- `CLAUDE.md` -> Code Conventions -> TypeScript
