/**
 * What the session screen is showing, derived from the two things that move.
 *
 * This is a pure function and separate from the screen for one reason: the
 * central requirement of TASK-025 — that a provider is never shown a visit in
 * progress while capture is in an error state — is a property of a mapping, and
 * a mapping can be tested exhaustively over every code in the vocabulary. Left
 * inline in a component it would be a property of whichever branches someone
 * remembered to write.
 *
 * The guarantee, stated precisely: `recording` is returned only when the capture
 * hook reports `streaming`. There is no other path to it. A provider who
 * believes an encounter is being recorded when it is not loses the transcript,
 * the SOAP note and every nudge that should have fired, with nothing on screen
 * ever having said so — which is worse than a visit that refuses to start.
 */

import type { AudioCaptureError } from '@medauth/audio-wire';

import type { AudioCaptureState } from '../hooks/useAudioCapture';
import type { Session } from '../api/sessions';

/** Where the encounter itself is, independent of the microphone. */
export type SessionStatus =
  | { kind: 'none' }
  /** `POST /sessions/start` is in flight. */
  | { kind: 'creating' }
  | { kind: 'open'; session: Session }
  | { kind: 'ending'; session: Session }
  | { kind: 'ended' }
  /** The visit could not be started, or a re-mint was refused. */
  | { kind: 'failed'; message: string }
  /** Capture is stopped but `POST /sessions/{id}/end` did not succeed. */
  | { kind: 'end-failed'; session: Session; message: string };

export type VisitPhase =
  | { kind: 'idle' }
  | { kind: 'starting' }
  /** The encounter exists; the microphone and socket are still coming up. */
  | { kind: 'connecting' }
  | { kind: 'recording' }
  | { kind: 'ending' }
  | { kind: 'ended' }
  | { kind: 'capture-failed'; error: AudioCaptureError }
  | { kind: 'visit-failed'; message: string };

export function visitPhase(session: SessionStatus, capture: AudioCaptureState): VisitPhase {
  // Deliberate outcomes of a provider's own action come first. Once a visit has
  // been ended, or has failed before a microphone was ever involved, a capture
  // error is not the thing to put in front of them — and `stop()` returns the
  // hook to `idle` anyway, so this ordering matters only in the moments around
  // teardown.
  switch (session.kind) {
    case 'ending':
      return { kind: 'ending' };
    case 'ended':
      return { kind: 'ended' };
    case 'failed':
    case 'end-failed':
      return { kind: 'visit-failed', message: session.message };
    default:
      break;
  }

  if (capture.status === 'error') {
    return { kind: 'capture-failed', error: capture.error };
  }

  switch (session.kind) {
    case 'none':
      return { kind: 'idle' };
    case 'creating':
      return { kind: 'starting' };
    case 'open':
      // The one place `recording` is produced, and it asks the hook rather than
      // inferring from the session having been created.
      return capture.status === 'streaming' ? { kind: 'recording' } : { kind: 'connecting' };
    default: {
      const unhandled: never = session;
      void unhandled;
      return { kind: 'idle' };
    }
  }
}
