/**
 * `useAudioCapture` — encounter audio from the browser to audio-ingestion.
 *
 * The capture graph is `getUserMedia` -> `AudioContext({ sampleRate: 16000 })`
 * -> `AudioWorkletNode`, and each piece is there for a reason that MediaRecorder
 * could not satisfy: MediaRecorder emits container-framed encoded audio, gives
 * no sample-rate control, and produces `timeslice` chunks that are not
 * independently decodable. See TASK-023 for the measurements. The `AudioContext`
 * is also the only resampler involved — devices commonly capture at 48kHz, and
 * this is what turns that into the 16kHz Transcribe Medical is configured for.
 *
 * The order of operations is the point of this module, not an implementation
 * detail. TASK-020 established that Transcribe answers a disagreeing sample rate
 * by hanging rather than erroring, so:
 *
 *   permission -> open the context -> compare the rate it reports
 *   -> compare the first worklet message -> only then open the WebSocket
 *
 * A mismatch therefore means no socket was ever opened and not one byte of
 * audio left the browser. The comparison happens twice on purpose, mirroring
 * TASK-022: the context's rate does not depend on audio ever arriving, and the
 * first message is the audio that would really be sent.
 *
 * Nothing here throws. State is a discriminated union carrying a typed error,
 * per CLAUDE.md's "errors bubble up as typed Result objects, not thrown
 * exceptions" — load-bearing rather than stylistic, because a thrown error is
 * precisely what a React error boundary would swallow.
 *
 * PHI discipline: audio lives only in the framer's buffer and is dropped on
 * every exit path. The session JWT rides in the subprotocol list, never in the
 * URL and never in a log line.
 */

import {
  CHANNELS,
  PcmFramer,
  PendingAudioOverflow,
  SAMPLE_RATE_HZ,
  floatToInt16LE,
  formatMismatch,
  type AudioCaptureError,
} from '@medauth/audio-wire';
import { useCallback, useEffect, useRef, useState } from 'react';

import processorUrl from '../audio/pcm-capture-processor.js?url';

/** WebSocket readyState OPEN. Spelled out so the check reads without the DOM enum. */
const SOCKET_OPEN = 1;

/** The processor name registered by `pcm-capture-processor.js`. */
const PROCESSOR_NAME = 'pcm-capture';

/**
 * How long after the graph is built the first quantum may take to arrive.
 *
 * **A round-number default, not a measured threshold.** What was measured, in
 * Chrome, is the other end of the scale: once a context is running the first
 * quantum arrives within one render quantum, about 8ms at 16kHz. Three seconds
 * is therefore several hundred times any healthy start, chosen to sit far above
 * it while still being shorter than a provider would tolerate staring at a
 * screen that has not admitted anything is wrong.
 *
 * It exists because the failure it catches is silent by construction: a
 * suspended `AudioContext` reports no error and simply never runs the worklet,
 * so the only evidence is audio that does not arrive.
 */
export const FIRST_AUDIO_TIMEOUT_MS = 3_000;

/**
 * The subprotocol list, and the reason there is one.
 *
 * The native `WebSocket` constructor takes a URL and subprotocols and nothing
 * else — there is no header carrier available to a browser, which is exactly
 * why CLAUDE.md defines two. The version marker is offered first so the server
 * has something safe to echo: selecting the `medauth.jwt.` entry instead would
 * write the credential into the handshake response and from there into every
 * proxy log on the path.
 */
function subprotocols(jwt: string): string[] {
  return ['medauth.session.v1', `medauth.jwt.${jwt}`];
}

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

/** What the worklet posts on every render quantum. */
interface CaptureMessage {
  sampleRate: number;
  channels: number;
  samples: Float32Array;
}

export type AudioCaptureState =
  | { status: 'idle' }
  | { status: 'requesting-permission' }
  | { status: 'starting' }
  | { status: 'streaming' }
  | { status: 'error'; error: AudioCaptureError };

export interface UseAudioCaptureOptions {
  /** The encounter's session id, from `POST /sessions/start` (TASK-006). */
  sessionId: string;
  /** The session JWT from the same response. Carried as a subprotocol, never logged. */
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
 * Whether a `getUserMedia` rejection means the provider (or the OS, or the
 * page's permissions policy) said no, as opposed to the microphone failing.
 *
 * The distinction reaches a person: TASK-070 offers a route into browser
 * settings for one and a plain retry for the other. The error's `message` is
 * never surfaced — it can name device paths and adds nothing actionable.
 */
function isPermissionDenial(error: unknown): boolean {
  const name = error instanceof Error ? error.name : '';
  return name === 'NotAllowedError' || name === 'SecurityError';
}

/**
 * The rate this device would capture at, for reporting only.
 *
 * Called when `new AudioContext({ sampleRate })` is rejected outright, so the
 * error a provider sees can say what the hardware does rather than just that
 * something did not match. Returns 0 if even a default context cannot be built,
 * which `formatMismatch` reports as plainly as any other wrong number.
 */
function probeDeviceSampleRate(): number {
  try {
    const probe = new AudioContext();
    const rate = probe.sampleRate;
    void probe.close();
    return rate;
  } catch {
    return 0;
  }
}

export function useAudioCapture({ sessionId, jwt, baseUrl }: UseAudioCaptureOptions): AudioCapture {
  const [state, setState] = useState<AudioCaptureState>({ status: 'idle' });

  const framerRef = useRef<PcmFramer>(new PcmFramer());
  const socketRef = useRef<WebSocket | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const nodeRef = useRef<AudioWorkletNode | null>(null);
  const sinkRef = useRef<GainNode | null>(null);
  const firstAudioTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const validatedRef = useRef(false);
  const openedRef = useRef(false);
  const runningRef = useRef(false);

  const teardown = useCallback(() => {
    if (firstAudioTimerRef.current !== null) {
      clearTimeout(firstAudioTimerRef.current);
      firstAudioTimerRef.current = null;
    }
    runningRef.current = false;
    validatedRef.current = false;
    openedRef.current = false;

    const node = nodeRef.current;
    if (node) {
      // Detached before disconnecting: a quantum already in flight would
      // otherwise arrive after teardown and push audio into a cleared framer.
      node.port.onmessage = null;
      node.disconnect();
      nodeRef.current = null;
    }

    sinkRef.current?.disconnect();
    sinkRef.current = null;

    // Every track stopped, not just the stream dropped — an un-stopped track
    // leaves the browser's recording indicator on after the encounter ends.
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;

    void contextRef.current?.close();
    contextRef.current = null;

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
    // The token is a subprotocol value. Never the query string — that is the one
    // place a credential is certain to be written to an intermediary's logs.
    const socket = new WebSocket(`${baseUrl}/ws/audio/${sessionId}`, subprotocols(jwt));

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

  const handleMessage = useCallback(
    (message: CaptureMessage) => {
      if (!runningRef.current) {
        return;
      }

      if (!validatedRef.current) {
        // The worklet reports the rate its own global scope is rendering at and
        // how many channels actually arrived — the audio that would really reach
        // Transcribe. Checked even though the context's rate already matched.
        const mismatch = formatMismatch({
          sampleRate: message.sampleRate,
          channels: message.channels,
        });
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
        framerRef.current.push(floatToInt16LE(message.samples));
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

  const start = useCallback(async () => {
    if (runningRef.current) {
      return;
    }

    setState({ status: 'requesting-permission' });
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: CHANNELS,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (error) {
      fail(
        isPermissionDenial(error)
          ? {
              code: 'PERMISSION_DENIED',
              message: 'MedAuth AI needs microphone access to record this encounter.',
            }
          : { code: 'CAPTURE_FAILED', message: 'The microphone could not be started.' },
      );
      return;
    }

    setState({ status: 'starting' });
    framerRef.current.clear();
    validatedRef.current = false;
    openedRef.current = false;
    runningRef.current = true;
    streamRef.current = stream;

    let context: AudioContext;
    try {
      context = new AudioContext({ sampleRate: SAMPLE_RATE_HZ });
    } catch {
      // A browser that cannot resample to 16kHz rejects the request outright
      // rather than quietly giving a different rate. Report what it does do.
      fail({
        code: 'SAMPLE_RATE_UNSUPPORTED',
        message: `This browser could not capture audio at ${SAMPLE_RATE_HZ} Hz, which MedAuth AI requires.`,
        detail: {
          requested: { sampleRate: SAMPLE_RATE_HZ, channels: CHANNELS },
          actual: { sampleRate: probeDeviceSampleRate(), channels: CHANNELS },
        },
      });
      return;
    }
    contextRef.current = context;

    // First of the two checks. The channel count comes from the track rather
    // than the context, and a browser that reports none is not treated as a
    // mismatch — the worklet's own count settles it either way.
    const settings = stream.getAudioTracks()[0]?.getSettings();
    const mismatch = formatMismatch({
      sampleRate: context.sampleRate,
      channels: settings?.channelCount ?? CHANNELS,
    });
    if (mismatch) {
      fail(mismatch);
      return;
    }

    // A context created without sticky user activation starts suspended, and a
    // suspended context never runs the worklet. `resume()` is *not* awaited:
    // without activation its promise may never settle at all — observed
    // directly, a bare `await resume()` on a page with no prior interaction
    // simply hangs — so awaiting it here would trade a silent hang inside Web
    // Audio for a silent hang inside `start()`. The request is made, and the
    // deadline below is what decides whether it worked.
    if (context.state === 'suspended') {
      void context.resume().catch(() => {
        // Rejection is not itself the failure; no audio arriving is, and that
        // is what the deadline reports. Swallowed so it cannot surface as an
        // unhandled rejection carrying a device-specific message.
      });
    }

    try {
      await context.audioWorklet.addModule(processorUrl);
    } catch {
      fail({ code: 'CAPTURE_FAILED', message: 'The microphone could not be started.' });
      return;
    }

    // `start()` awaited twice above, so the hook may have been stopped or
    // unmounted in the meantime. Building the graph now would leave a live
    // microphone behind a torn-down hook.
    if (!runningRef.current) {
      return;
    }

    const node = new AudioWorkletNode(context, PROCESSOR_NAME, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [CHANNELS],
      channelCount: CHANNELS,
      channelCountMode: 'explicit',
    });
    node.port.onmessage = (event: MessageEvent<CaptureMessage>) => {
      handleMessage(event.data);
    };

    // The graph reaches `destination` through a silent gain stage, and both
    // halves of that matter. A node has to be reachable from the destination to
    // be guaranteed a `process()` call: a node with no outputs is *specified* to
    // keep processing on its inputs alone, and Chrome does, but that is one
    // engine — and a browser that disagreed would produce no audio, no error and
    // no way to tell from here. Routing to the destination is what every engine
    // pulls. The gain of zero is what stops that from playing the encounter back
    // through the room's speakers; the processor writes nothing to its output
    // either, so the silence is not resting on the gain value alone.
    const sink = context.createGain();
    sink.gain.value = 0;
    node.connect(sink);
    sink.connect(context.destination);
    context.createMediaStreamSource(stream).connect(node);

    sinkRef.current = sink;
    nodeRef.current = node;

    // Everything upstream reported success; from here the only evidence that
    // capture is really running is a quantum arriving. Cleared by the first one.
    firstAudioTimerRef.current = setTimeout(() => {
      firstAudioTimerRef.current = null;
      fail({
        code: 'CAPTURE_TIMED_OUT',
        message: 'The microphone started but no audio reached MedAuth AI. Nothing was recorded.',
      });
    }, FIRST_AUDIO_TIMEOUT_MS);
  }, [fail, handleMessage]);

  const stop = useCallback(() => {
    teardown();
    setState({ status: 'idle' });
  }, [teardown]);

  // Unmounting mid-encounter must not leave the microphone open or audio held.
  useEffect(() => () => teardown(), [teardown]);

  return { state, start, stop };
}
