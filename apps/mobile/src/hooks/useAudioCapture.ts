/**
 * `useAudioCapture` — encounter audio from the microphone to audio-ingestion.
 *
 * The order of operations is the point of this module, not an implementation
 * detail. `expo-audio` reports the sample rate it *actually* captured at, which
 * may differ from the one requested if the hardware cannot oblige, and TASK-020
 * established that Transcribe Medical answers a disagreeing rate by hanging
 * rather than erroring. So:
 *
 *   permission -> start stream -> compare the rate the stream reports
 *   -> compare the first buffer delivered -> only then open the WebSocket
 *
 * A mismatch therefore means no socket was ever opened and not one byte of
 * audio left the device.
 *
 * The comparison happens twice on purpose. `AudioStream` publishes the actual
 * rate once started, which catches the mismatch without depending on a buffer
 * ever arriving; the first buffer is checked as well because it is the audio
 * that would really reach Transcribe, and the two disagreeing is itself a
 * reason not to stream.
 *
 * A deadline bounds the wait for that first buffer. Everything upstream of it
 * can report success and still deliver nothing — a microphone seized by another
 * app, an input route changing mid-start — and without a bound the hook would
 * sit in `starting` forever, telling the provider nothing while the encounter
 * goes unrecorded. That silent hang is the failure this module exists to make
 * impossible, so the quiet version of it is not allowed either.
 *
 * Nothing here throws. State is a discriminated union carrying a typed error,
 * per CLAUDE.md's "errors bubble up as typed Result objects, not thrown
 * exceptions" — load-bearing rather than stylistic, because a thrown error is
 * precisely what a React error boundary would swallow.
 *
 * PHI discipline: audio lives only in the framer's buffer and is dropped on
 * every exit path. The session JWT goes in a header, never in the URL and never
 * in a log line.
 */

import type { AudioStream, AudioStreamBuffer } from 'expo-audio';
import { requestRecordingPermissionsAsync, useAudioStream } from 'expo-audio';
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  CHANNELS,
  ENCODING,
  PcmFramer,
  PendingAudioOverflow,
  SAMPLE_RATE_HZ,
  formatMismatch,
  formatOf,
  isLittleEndian,
  type AudioCaptureError,
} from '@medauth/audio-wire';

/** WebSocket readyState OPEN. Spelled out so the check reads without the DOM enum. */
const SOCKET_OPEN = 1;

/**
 * How long after `stream.start()` resolves the first buffer may take to arrive.
 *
 * **A round-number default, not a measured value**, in the same sense as
 * `MAX_PENDING_BYTES`: no device start-up latencies have been observed yet,
 * because there is no session screen on this platform to produce them until
 * TASK-025. Treat it as a placeholder for that measurement.
 *
 * It is deliberately not the browser's 3s. That figure is anchored to something
 * measured — once an `AudioContext` is running the first quantum arrives within
 * about one render quantum, ~8ms — and the whole path it bounds sits inside the
 * renderer. Nothing here is comparable: `stream.start()` resolving means the OS
 * audio subsystem has accepted the request, not that an input route is live, and
 * where the provider is wearing a Bluetooth headset that route still has to be
 * negotiated. Reusing 3s would import a justification that is not about this
 * platform.
 *
 * Eight seconds because the two ways of being wrong do not cost the same. Too
 * short tears down a capture that was about to work and tells the provider
 * nothing was recorded — on hardware that is reliably slow to start, that is a
 * product which never records at all, and a retry does not help. Too long only
 * makes the provider wait longer for an error that is still actionable. So err
 * long, bounded by the thing that actually matters: a provider must not be left
 * watching a screen that has not admitted anything is wrong.
 */
export const FIRST_AUDIO_TIMEOUT_MS = 8_000;

/**
 * The one message for a connection that never opened.
 *
 * TASK-020 validates the session token *before* completing the handshake, so a
 * rejected JWT arrives as a failed upgrade rather than a close with code 4401.
 * There is nothing to distinguish it from an unreachable host at this layer, and
 * of the two the actionable one is the token — so say so, and say what to do.
 */
const REFUSED: AudioCaptureError = {
  code: 'AUTH_REJECTED',
  message:
    'The audio connection was refused. The session token is expired or invalid; start a new session.',
};

export type AudioCaptureState =
  | { status: 'idle' }
  | { status: 'requesting-permission' }
  | { status: 'starting' }
  | { status: 'streaming' }
  | { status: 'error'; error: AudioCaptureError };

export interface UseAudioCaptureOptions {
  /** The encounter's session id, from `POST /sessions/start` (TASK-006). */
  sessionId: string;
  /** The session JWT from the same response. Carried as a header, never logged. */
  jwt: string;
  /** WebSocket origin for audio-ingestion, e.g. `wss://audio.example`. */
  baseUrl: string;
}

export interface AudioCapture {
  state: AudioCaptureState;
  /** Begin capture. Safe to call when already running — it is a no-op. */
  start: () => Promise<void>;
  /** Stop capture, close the socket and drop buffered audio. */
  stop: () => void;
}

/**
 * React Native's WebSocket takes a third options argument the DOM's does not,
 * which is how mobile can use the `Authorization` header carrier that
 * `apps/web` cannot (see CLAUDE.md, "How the JWT reaches a WebSocket
 * endpoint"). Declared locally so the call site stays type-checked.
 */
type WebSocketWithHeaders = new (
  url: string,
  protocols: string[] | undefined,
  options: { headers: Record<string, string> },
) => WebSocket;

export function useAudioCapture({
  sessionId,
  jwt,
  baseUrl,
}: UseAudioCaptureOptions): AudioCapture {
  const [state, setState] = useState<AudioCaptureState>({ status: 'idle' });

  const framerRef = useRef<PcmFramer>(new PcmFramer());
  const socketRef = useRef<WebSocket | null>(null);
  const validatedRef = useRef(false);
  const openedRef = useRef(false);
  const runningRef = useRef(false);
  const streamRef = useRef<AudioStream | null>(null);
  const firstAudioTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const teardown = useCallback(() => {
    // First, so every failure path and the unmount cleanup all disarm the
    // deadline — a hook that has already stopped must not fail seconds later.
    if (firstAudioTimerRef.current !== null) {
      clearTimeout(firstAudioTimerRef.current);
      firstAudioTimerRef.current = null;
    }
    runningRef.current = false;
    validatedRef.current = false;
    openedRef.current = false;
    try {
      streamRef.current?.stop();
    } catch {
      // Teardown runs from effect cleanup and from every failure path, where a
      // throw would replace the failure being reported with a less informative
      // one. Already-stopped is the state we were asking for anyway.
    }
    socketRef.current?.close();
    socketRef.current = null;
    // Audio never outlives the capture that produced it.
    framerRef.current.clear();
  }, []);

  const fail = useCallback(
    (error: AudioCaptureError) => {
      teardown();
      setState({ status: 'error', error });
    },
    [teardown],
  );

  const flush = useCallback(() => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== SOCKET_OPEN) {
      return;
    }
    for (const frame of framerRef.current.takeFrames()) {
      socket.send(frame);
    }
  }, []);

  const openSocket = useCallback(() => {
    // The token is a header value. Never the query string — that is the one
    // place a credential is certain to be written to an intermediary's logs.
    const url = `${baseUrl}/ws/audio/${sessionId}`;
    const socket = new (WebSocket as unknown as WebSocketWithHeaders)(url, undefined, {
      headers: { Authorization: `Bearer ${jwt}` },
    });

    socket.onopen = () => {
      openedRef.current = true;
      setState({ status: 'streaming' });
      flush();
    };

    socket.onclose = () => {
      if (!runningRef.current) {
        return;
      }
      // A socket that never opened is a refused upgrade, which is how TASK-020
      // rejects a bad token: it validates before completing the handshake, so
      // there is no frame to carry a 4401 in. Re-mint before retrying.
      fail(
        openedRef.current
          ? { code: 'STREAM_FAILED', message: 'The audio connection closed unexpectedly.' }
          : REFUSED,
      );
    };

    socket.onerror = () => {
      if (!runningRef.current || openedRef.current) {
        return;
      }
      // Nothing from the event is logged: it can carry the request URL.
      fail(REFUSED);
    };

    socketRef.current = socket;
  }, [baseUrl, sessionId, jwt, flush, fail]);

  const handleBuffer = useCallback(
    (buffer: AudioStreamBuffer) => {
      if (!runningRef.current) {
        return;
      }

      if (!validatedRef.current) {
        // The delivered buffer is the authoritative format — this is what would
        // actually reach Transcribe — so it is checked even though `start()`
        // already compared the rate the stream reported.
        const mismatch = formatMismatch(formatOf(buffer));
        if (mismatch) {
          fail(mismatch);
          return;
        }
        validatedRef.current = true;
        if (firstAudioTimerRef.current !== null) {
          clearTimeout(firstAudioTimerRef.current);
          firstAudioTimerRef.current = null;
        }
        openSocket();
      }

      try {
        framerRef.current.push(buffer.data);
      } catch (error) {
        if (error instanceof PendingAudioOverflow) {
          fail({
            code: 'SEND_BACKLOG_EXCEEDED',
            message:
              'The audio connection could not be established. No part of this encounter was recorded.',
          });
          return;
        }
        throw error;
      }
      flush();
    },
    [fail, flush, openSocket],
  );

  const { stream } = useAudioStream({
    sampleRate: SAMPLE_RATE_HZ,
    channels: CHANNELS,
    encoding: ENCODING,
    onBuffer: handleBuffer,
  });

  // Held in a ref so `teardown` can stop the microphone without taking `stream`
  // as a dependency — it is the unmount cleanup, and re-running it whenever the
  // stream object's identity changed would stop a capture that is still wanted.
  // Assigned in an effect rather than during render: writing a ref while
  // rendering is what `react-hooks/refs` forbids.
  useEffect(() => {
    streamRef.current = stream;
  }, [stream]);

  const start = useCallback(async () => {
    if (runningRef.current) {
      return;
    }

    if (!isLittleEndian()) {
      fail({
        code: 'ENDIANNESS_UNSUPPORTED',
        message: 'This device stores audio samples in an order Transcribe Medical cannot read.',
      });
      return;
    }

    setState({ status: 'requesting-permission' });
    const permission = await requestRecordingPermissionsAsync();
    if (!permission.granted) {
      fail({
        code: 'PERMISSION_DENIED',
        message: 'MedAuth AI needs microphone access to record this encounter.',
      });
      return;
    }

    setState({ status: 'starting' });
    framerRef.current.clear();
    validatedRef.current = false;
    openedRef.current = false;
    runningRef.current = true;

    try {
      await stream.start();
    } catch {
      // The underlying error is not surfaced: it can name device paths and adds
      // nothing a provider can act on.
      fail({ code: 'CAPTURE_FAILED', message: 'The microphone could not be started.' });
      return;
    }

    // `AudioStream` publishes the rate it is actually delivering once started,
    // so the mismatch can be caught here rather than waiting for a buffer that
    // may never arrive. Zero means the native side has not filled it in yet;
    // that is not a mismatch, and the first buffer settles it either way.
    const reported = { sampleRate: stream.sampleRate, channels: stream.channels };
    if (reported.sampleRate) {
      const mismatch = formatMismatch(reported);
      if (mismatch) {
        fail(mismatch);
        return;
      }
    }

    // Everything upstream reported success; from here the only evidence that
    // capture is really running is a buffer arriving. Cleared by the first one.
    firstAudioTimerRef.current = setTimeout(() => {
      firstAudioTimerRef.current = null;
      fail({
        code: 'CAPTURE_TIMED_OUT',
        message: 'The microphone started but no audio reached MedAuth AI. Nothing was recorded.',
      });
    }, FIRST_AUDIO_TIMEOUT_MS);
  }, [fail, stream]);

  const stop = useCallback(() => {
    teardown();
    setState({ status: 'idle' });
  }, [teardown]);

  // Unmounting mid-encounter must not leave the microphone open or audio held.
  useEffect(() => () => teardown(), [teardown]);

  return { state, start, stop };
}
