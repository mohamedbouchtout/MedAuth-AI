import type { AudioCaptureError, AudioCaptureErrorCode } from '@medauth/audio-wire';

import type { AudioCaptureState } from '../../../src/hooks/useAudioCapture';
import { visitPhase, type SessionStatus } from '../../../src/session/visitPhase';

const SESSION = { sessionId: '11111111-1111-4111-8111-111111111111', jwt: 'a.b.c' };

/** Every code in the vocabulary; `satisfies` fails the build if one is added. */
const ALL_CODES = Object.keys({
  PERMISSION_DENIED: true,
  SAMPLE_RATE_UNSUPPORTED: true,
  CHANNELS_UNSUPPORTED: true,
  ENDIANNESS_UNSUPPORTED: true,
  CAPTURE_FAILED: true,
  CAPTURE_TIMED_OUT: true,
  AUTH_REJECTED: true,
  SEND_BACKLOG_EXCEEDED: true,
  STREAM_FAILED: true,
} satisfies Record<AudioCaptureErrorCode, true>) as AudioCaptureErrorCode[];

function captureError(code: AudioCaptureErrorCode): AudioCaptureState {
  const error: AudioCaptureError = { code, message: `failed: ${code}` };
  return { status: 'error', error };
}

const OPEN: SessionStatus = { kind: 'open', session: SESSION };

describe('visitPhase — the visit is never in progress while capture has failed', () => {
  it.each(ALL_CODES)('reports capture-failed rather than recording for %s', (code) => {
    const phase = visitPhase(OPEN, captureError(code));

    expect(phase.kind).toBe('capture-failed');
    expect(phase).not.toEqual(expect.objectContaining({ kind: 'recording' }));
  });

  it.each(ALL_CODES)('surfaces the error for %s while the session is still being created', (code) => {
    expect(visitPhase({ kind: 'creating' }, captureError(code)).kind).toBe('capture-failed');
  });

  it('carries the error through so the screen can say what happened', () => {
    const phase = visitPhase(OPEN, captureError('CAPTURE_TIMED_OUT'));

    expect(phase).toEqual({
      kind: 'capture-failed',
      error: { code: 'CAPTURE_TIMED_OUT', message: 'failed: CAPTURE_TIMED_OUT' },
    });
  });

  it.each([
    ['idle', { status: 'idle' } as AudioCaptureState],
    ['requesting-permission', { status: 'requesting-permission' } as AudioCaptureState],
    ['starting', { status: 'starting' } as AudioCaptureState],
  ])('reports connecting, not recording, while capture is %s', (_label, capture) => {
    // An open encounter is not a recording one. The only evidence that audio is
    // reaching the server is the hook saying so.
    expect(visitPhase(OPEN, capture).kind).toBe('connecting');
  });

  it('reports recording only once capture is streaming', () => {
    expect(visitPhase(OPEN, { status: 'streaming' }).kind).toBe('recording');
  });
});

describe('visitPhase — session states', () => {
  it.each([
    ['none', { kind: 'none' } as SessionStatus, 'idle'],
    ['creating', { kind: 'creating' } as SessionStatus, 'starting'],
    ['ending', { kind: 'ending', session: SESSION } as SessionStatus, 'ending'],
    ['ended', { kind: 'ended' } as SessionStatus, 'ended'],
  ])('maps %s to %s', (_label, session, expected) => {
    expect(visitPhase(session, { status: 'idle' }).kind).toBe(expected);
  });

  it('reports a start failure with its message', () => {
    expect(visitPhase({ kind: 'failed', message: 'no patient' }, { status: 'idle' })).toEqual({
      kind: 'visit-failed',
      message: 'no patient',
    });
  });

  it('reports a failed end with its message', () => {
    expect(
      visitPhase({ kind: 'end-failed', session: SESSION, message: 'could not close' }, {
        status: 'idle',
      }),
    ).toEqual({ kind: 'visit-failed', message: 'could not close' });
  });

  it('does not show a capture error over a visit the provider deliberately ended', () => {
    // Ending stops capture, so this is about the moments around teardown: the
    // answer to "what happened" is "you ended the visit", not a stream error.
    const phase = visitPhase({ kind: 'ending', session: SESSION }, captureError('STREAM_FAILED'));

    expect(phase.kind).toBe('ending');
  });
});
