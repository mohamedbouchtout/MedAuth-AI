import { CHUNK_BYTES, MAX_PENDING_BYTES, SAMPLE_RATE_HZ } from '@medauth/audio-wire';
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FIRST_AUDIO_TIMEOUT_MS, useAudioCapture } from '../../../src/hooks/useAudioCapture';

/**
 * `AudioWorklet` does not exist in jsdom and cannot be shimmed meaningfully —
 * it is a second global scope with its own render thread. So the fakes stop at
 * the boundary the hook actually depends on: an `AudioContext` that reports a
 * sample rate and resolves `addModule`, and a node whose `port` the test drives
 * directly. Everything the real processor would decide is deliberately not in
 * the processor (see `pcm-capture-processor.js`), so nothing is lost by this.
 *
 * The conversion and framing the hook applies to these messages are tested as
 * pure functions in `packages/audio-wire`, not here.
 */

const OPTIONS = {
  sessionId: '11111111-1111-4111-8111-111111111111',
  jwt: 'header.payload.signature',
  baseUrl: 'wss://audio.example',
};

interface CaptureMessage {
  sampleRate: number;
  channels: number;
  samples: Float32Array;
}

/** One worklet message carrying `sampleCount` samples of non-silent audio. */
function quantum(sampleCount: number, overrides: Partial<CaptureMessage> = {}): CaptureMessage {
  const samples = new Float32Array(sampleCount);
  samples.fill(0.5);
  return { sampleRate: SAMPLE_RATE_HZ, channels: 1, samples, ...overrides };
}

/** 4000 float samples convert to exactly one 8000-byte frame. */
const SAMPLES_PER_FRAME = CHUNK_BYTES / 2;

class FakeMediaStreamTrack {
  stopped = false;
  constructor(private readonly settings: MediaTrackSettings) {}
  getSettings(): MediaTrackSettings {
    return this.settings;
  }
  stop(): void {
    this.stopped = true;
  }
}

class FakeMediaStream {
  constructor(readonly tracks: FakeMediaStreamTrack[]) {}
  getTracks(): FakeMediaStreamTrack[] {
    return this.tracks;
  }
  getAudioTracks(): FakeMediaStreamTrack[] {
    return this.tracks;
  }
}

class FakeGainNode {
  gain = { value: 1 };
  connectedTo: unknown[] = [];
  disconnected = false;

  connect(target: unknown): void {
    this.connectedTo.push(target);
  }

  disconnect(): void {
    this.disconnected = true;
  }
}

class FakeAudioWorkletNode {
  static instances: FakeAudioWorkletNode[] = [];
  static shouldThrow = false;

  disconnected = false;
  port: { onmessage: ((event: { data: CaptureMessage }) => void) | null } = { onmessage: null };

  constructor(
    readonly context: unknown,
    readonly name: string,
    readonly options: Record<string, unknown>,
  ) {
    if (FakeAudioWorkletNode.shouldThrow) {
      throw new Error('node construction failed');
    }
    FakeAudioWorkletNode.instances.push(this);
  }

  connectedTo: unknown[] = [];

  connect(target: unknown): void {
    this.connectedTo.push(target);
  }

  disconnect(): void {
    this.disconnected = true;
  }

  /** Deliver one worklet message, as the real port would. */
  deliver(message: CaptureMessage): void {
    this.port.onmessage?.({ data: message });
  }
}

class FakeAudioContext {
  static instances: FakeAudioContext[] = [];
  /** What the constructor honours when a rate is requested; null throws. */
  static grantedSampleRate: number | null = SAMPLE_RATE_HZ;
  /** What a default (no-argument) context reports, i.e. the device's own rate. */
  static deviceSampleRate = 48_000;
  static addModuleRejects = false;
  /** When set, `addModule` parks on this until a test releases it. */
  static addModuleGate: Promise<void> | null = null;
  /** What `state` reads as on construction. */
  static initialState: 'running' | 'suspended' = 'running';
  /** When true, `resume()` returns a promise that never settles — as it does
   *  in a real browser with no user activation. */
  static resumeNeverSettles = false;

  readonly sampleRate: number;
  readonly destination = { id: 'destination' };
  state: 'running' | 'suspended' = FakeAudioContext.initialState;
  resumeCalls = 0;
  gains: FakeGainNode[] = [];
  closed = false;
  connectedSources = 0;
  addedModules: string[] = [];

  resume(): Promise<void> {
    this.resumeCalls += 1;
    if (FakeAudioContext.resumeNeverSettles) {
      return new Promise<void>(() => {});
    }
    this.state = 'running';
    return Promise.resolve();
  }

  createGain(): FakeGainNode {
    const gain = new FakeGainNode();
    this.gains.push(gain);
    return gain;
  }

  audioWorklet = {
    addModule: async (url: string): Promise<void> => {
      if (FakeAudioContext.addModuleRejects) {
        throw new Error('addModule failed');
      }
      if (FakeAudioContext.addModuleGate) {
        await FakeAudioContext.addModuleGate;
      }
      this.addedModules.push(url);
    },
  };

  constructor(options?: { sampleRate?: number }) {
    if (options?.sampleRate !== undefined) {
      if (FakeAudioContext.grantedSampleRate === null) {
        throw new Error('sample rate not supported');
      }
      this.sampleRate = FakeAudioContext.grantedSampleRate;
    } else {
      this.sampleRate = FakeAudioContext.deviceSampleRate;
    }
    FakeAudioContext.instances.push(this);
  }

  createMediaStreamSource(): { connect: () => void } {
    return {
      connect: () => {
        this.connectedSources += 1;
      },
    };
  }

  async close(): Promise<void> {
    this.closed = true;
  }
}

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  readyState = 0;
  sent: Uint8Array[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(
    readonly url: string,
    readonly protocols: string[],
  ) {
    FakeWebSocket.instances.push(this);
  }

  send(frame: Uint8Array): void {
    this.sent.push(frame);
  }

  close(): void {
    this.closed = true;
  }

  /** Complete the handshake, as a server accepting the upgrade would. */
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }
}

let getUserMedia: ReturnType<typeof vi.fn>;

/** The stream `getUserMedia` resolves with unless a test says otherwise. */
function grantMicrophone(settings: MediaTrackSettings = { channelCount: 1 }): void {
  getUserMedia.mockResolvedValue(new FakeMediaStream([new FakeMediaStreamTrack(settings)]));
}

/** Run `start()` and settle the promises it awaits. */
async function startCapture(start: () => Promise<void>): Promise<void> {
  await act(async () => {
    await start();
  });
}

function lastNode(): FakeAudioWorkletNode {
  const node = FakeAudioWorkletNode.instances.at(-1);
  if (!node) {
    throw new Error('no AudioWorkletNode was constructed');
  }
  return node;
}

function lastSocket(): FakeWebSocket {
  const socket = FakeWebSocket.instances.at(-1);
  if (!socket) {
    throw new Error('no WebSocket was constructed');
  }
  return socket;
}

beforeEach(() => {
  FakeAudioWorkletNode.instances = [];
  FakeAudioWorkletNode.shouldThrow = false;
  FakeAudioContext.instances = [];
  FakeAudioContext.grantedSampleRate = SAMPLE_RATE_HZ;
  FakeAudioContext.deviceSampleRate = 48_000;
  FakeAudioContext.addModuleRejects = false;
  FakeAudioContext.addModuleGate = null;
  FakeAudioContext.initialState = 'running';
  FakeAudioContext.resumeNeverSettles = false;
  FakeWebSocket.instances = [];

  getUserMedia = vi.fn();
  grantMicrophone();

  vi.stubGlobal('AudioContext', FakeAudioContext);
  vi.stubGlobal('AudioWorkletNode', FakeAudioWorkletNode);
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function lastContext(): FakeAudioContext {
  const context = FakeAudioContext.instances.at(-1);
  if (!context) {
    throw new Error('no AudioContext was constructed');
  }
  return context;
}

describe('useAudioCapture — framing', () => {
  it('sends exactly one 8000-byte binary frame per frame of audio', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME));
    });
    act(() => {
      lastSocket().open();
    });
    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME));
    });

    const socket = lastSocket();
    // Two frames: the one buffered while connecting, then the one after open.
    expect(socket.sent).toHaveLength(2);
    expect(socket.sent.every((frame) => frame.length === CHUNK_BYTES)).toBe(true);
    expect(result.current.state.status).toBe('streaming');
  });

  it('holds a partial tail back rather than sending it early', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME / 2));
    });
    act(() => {
      lastSocket().open();
    });

    // Half a frame is half a frame; the rest arrives on the next quantum.
    expect(lastSocket().sent).toHaveLength(0);
  });

  it('flushes audio buffered while the socket was connecting', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME * 3));
    });
    expect(lastSocket().sent).toHaveLength(0);

    act(() => {
      lastSocket().open();
    });

    expect(lastSocket().sent).toHaveLength(3);
  });

  it('fails with SEND_BACKLOG_EXCEEDED when the socket never opens', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    await act(async () => {
      // One sample past the cap, delivered while the handshake never completes.
      lastNode().deliver(quantum(MAX_PENDING_BYTES / 2 + 2));
    });

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'SEND_BACKLOG_EXCEEDED' },
    });
  });
});

describe('useAudioCapture — format checks', () => {
  it('opens no socket when the context cannot give 16kHz', async () => {
    FakeAudioContext.grantedSampleRate = 44_100;
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    // The first of the two checks, and it runs before addModule — so no
    // worklet, no node, and nothing to send audio over.
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(FakeAudioWorkletNode.instances).toHaveLength(0);
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: {
        code: 'SAMPLE_RATE_UNSUPPORTED',
        detail: { actual: { sampleRate: 44_100 } },
      },
    });
  });

  it('reports the device rate when the context refuses the request outright', async () => {
    FakeAudioContext.grantedSampleRate = null;
    FakeAudioContext.deviceSampleRate = 48_000;
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    // A browser that rejects the rate rather than substituting one still owes
    // the provider a number, so the hook probes a default context for it.
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: {
        code: 'SAMPLE_RATE_UNSUPPORTED',
        detail: { actual: { sampleRate: 48_000 } },
      },
    });
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('opens no socket when the track reports more than one channel', async () => {
    grantMicrophone({ channelCount: 2 });
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'CHANNELS_UNSUPPORTED' },
    });
  });

  it('treats a track that reports no channel count as unsettled, not wrong', async () => {
    // Some browsers omit channelCount from getSettings(). The worklet's own
    // count settles it either way, so an absent value is not a mismatch.
    grantMicrophone({});
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    expect(result.current.state.status).toBe('starting');
    expect(FakeAudioWorkletNode.instances).toHaveLength(1);
  });

  it('opens no socket when the first worklet message disagrees', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    await act(async () => {
      // The context said 16kHz; the audio actually rendered at 48kHz. The two
      // disagreeing is itself a reason not to stream.
      lastNode().deliver(quantum(SAMPLES_PER_FRAME, { sampleRate: 48_000 }));
    });

    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'SAMPLE_RATE_UNSUPPORTED' },
    });
  });
});

describe('useAudioCapture — permission and startup', () => {
  it('reports PERMISSION_DENIED and opens no context', async () => {
    const denial = new Error('denied');
    denial.name = 'NotAllowedError';
    getUserMedia.mockRejectedValue(denial);
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    expect(FakeAudioContext.instances).toHaveLength(0);
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'PERMISSION_DENIED' },
    });
  });

  it('reports CAPTURE_FAILED when the microphone itself fails', async () => {
    const failure = new Error('device in use');
    failure.name = 'NotReadableError';
    getUserMedia.mockRejectedValue(failure);
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'CAPTURE_FAILED' },
    });
  });

  it('reports CAPTURE_FAILED when the worklet module cannot be loaded', async () => {
    FakeAudioContext.addModuleRejects = true;
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    expect(FakeAudioWorkletNode.instances).toHaveLength(0);
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'CAPTURE_FAILED' },
    });
  });

  it('routes the worklet to the destination through a silent gain', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    // Reaching the destination is what guarantees the node is pulled in every
    // engine, rather than relying on the no-outputs rule that only Chrome was
    // measured against. The zero gain is what keeps the encounter from playing
    // back into the room on the way there.
    const context = lastContext();
    const gain = context.gains[0];
    expect(lastNode().options).toMatchObject({
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
      channelCountMode: 'explicit',
    });
    expect(gain?.gain.value).toBe(0);
    expect(lastNode().connectedTo).toEqual([gain]);
    expect(gain?.connectedTo).toEqual([context.destination]);
    expect(context.connectedSources).toBe(1);
  });

  it('disconnects the gain stage on teardown', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);
    const gain = lastContext().gains[0];

    act(() => {
      result.current.stop();
    });

    expect(gain?.disconnected).toBe(true);
  });

  it('builds no graph when stopped while the worklet module is loading', async () => {
    // `start()` awaits twice, so a provider can end the visit mid-startup. The
    // graph must not be built afterwards — that would leave a live microphone
    // attached to a hook that believes it has stopped.
    let release: () => void = () => {};
    FakeAudioContext.addModuleGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    let pending: Promise<void> = Promise.resolve();
    await act(async () => {
      pending = result.current.start();
    });
    act(() => {
      result.current.stop();
    });
    await act(async () => {
      release();
      await pending;
    });

    expect(FakeAudioWorkletNode.instances).toHaveLength(0);
    expect(result.current.state.status).toBe('idle');
  });

  it('is a no-op when already running', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);
    await startCapture(result.current.start);

    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(FakeAudioWorkletNode.instances).toHaveLength(1);
  });
});

describe('useAudioCapture — the socket', () => {
  it('carries the session token as a subprotocol and never in the URL', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME));
    });

    const socket = lastSocket();
    expect(socket.url).toBe(`${OPTIONS.baseUrl}/ws/audio/${OPTIONS.sessionId}`);
    expect(socket.url).not.toContain(OPTIONS.jwt);
    // The version marker is offered first so the server has something safe to
    // echo; selecting the token entry would write it into the response headers.
    expect(socket.protocols).toEqual(['medauth.session.v1', `medauth.jwt.${OPTIONS.jwt}`]);
  });

  it('reads a connection that never opened as an auth failure', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);
    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME));
    });

    act(() => {
      // TASK-020 rejects a bad token before completing the handshake, so this
      // is what a 4401 looks like from a browser.
      lastSocket().onclose?.();
    });

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'AUTH_REJECTED' },
    });
  });

  it('reads an error before the handshake completes as an auth failure', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);
    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME));
    });

    act(() => {
      // A refused upgrade can surface as `error` rather than `close`, and it
      // means the same thing: the token, not the network.
      lastSocket().onerror?.();
    });

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'AUTH_REJECTED' },
    });
  });

  it('distinguishes a working connection that drops', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);
    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME));
    });
    const socket = lastSocket();
    act(() => {
      socket.open();
    });

    act(() => {
      socket.onclose?.();
    });

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'STREAM_FAILED' },
    });
  });

  it('ignores an error on a socket that had already opened', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);
    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME));
    });
    const socket = lastSocket();
    act(() => {
      socket.open();
    });

    act(() => {
      socket.onerror?.();
    });

    // The close handler reports it with the right code if it actually drops.
    expect(result.current.state.status).toBe('streaming');
  });
});

describe('useAudioCapture — a context that never runs', () => {
  it('asks a suspended context to resume', async () => {
    FakeAudioContext.initialState = 'suspended';
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    expect(lastContext().resumeCalls).toBe(1);
    expect(lastContext().state).toBe('running');
  });

  it('does not wait on resume, which may never settle', async () => {
    // With no user activation a real browser's resume() promise simply never
    // settles — observed directly. Awaiting it would move the silent hang from
    // Web Audio into start(), so start() has to finish regardless.
    FakeAudioContext.initialState = 'suspended';
    FakeAudioContext.resumeNeverSettles = true;
    const { result } = renderHook(() => useAudioCapture(OPTIONS));

    await startCapture(result.current.start);

    expect(lastContext().resumeCalls).toBe(1);
    expect(FakeAudioWorkletNode.instances).toHaveLength(1);
  });

  it('reports CAPTURE_TIMED_OUT when no audio ever arrives', async () => {
    vi.useFakeTimers();
    FakeAudioContext.initialState = 'suspended';
    FakeAudioContext.resumeNeverSettles = true;
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    // Everything reported success and nothing came. Without this deadline the
    // hook would sit in `starting` forever, which is the silent hang the whole
    // error vocabulary exists to prevent.
    expect(result.current.state.status).toBe('starting');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FIRST_AUDIO_TIMEOUT_MS);
    });

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'CAPTURE_TIMED_OUT' },
    });
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('cancels the deadline once a quantum arrives', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME));
    });
    act(() => {
      lastSocket().open();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FIRST_AUDIO_TIMEOUT_MS * 3);
    });

    // A working encounter must not be torn down by its own startup deadline.
    expect(result.current.state.status).toBe('streaming');
  });

  it('cancels the deadline on stop, so a stopped hook cannot fail late', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    act(() => {
      result.current.stop();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FIRST_AUDIO_TIMEOUT_MS * 3);
    });

    expect(result.current.state.status).toBe('idle');
  });
});

describe('useAudioCapture — teardown', () => {
  it('stops the microphone, closes the context and drops buffered audio', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);
    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME / 2));
    });
    const node = lastNode();
    const socket = lastSocket();
    const context = FakeAudioContext.instances[0];

    act(() => {
      result.current.stop();
    });

    expect(node.port.onmessage).toBeNull();
    expect(node.disconnected).toBe(true);
    expect(context?.closed).toBe(true);
    expect(socket.closed).toBe(true);
    expect(result.current.state.status).toBe('idle');

    // The held half-frame is gone: reopening and delivering the other half must
    // not complete a frame from audio captured before stop.
    await startCapture(result.current.start);
    await act(async () => {
      lastNode().deliver(quantum(SAMPLES_PER_FRAME / 2));
    });
    act(() => {
      lastSocket().open();
    });
    expect(lastSocket().sent).toHaveLength(0);
  });

  it('stops every track so the browser recording indicator goes out', async () => {
    const track = new FakeMediaStreamTrack({ channelCount: 1 });
    getUserMedia.mockResolvedValue(new FakeMediaStream([track]));
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);

    act(() => {
      result.current.stop();
    });

    expect(track.stopped).toBe(true);
  });

  it('tears down on unmount rather than leaving the microphone open', async () => {
    const { result, unmount } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);
    const node = lastNode();
    const context = FakeAudioContext.instances[0];

    unmount();

    expect(node.disconnected).toBe(true);
    expect(context?.closed).toBe(true);
  });

  it('ignores a quantum that arrives after teardown', async () => {
    const { result } = renderHook(() => useAudioCapture(OPTIONS));
    await startCapture(result.current.start);
    const node = lastNode();
    act(() => {
      result.current.stop();
    });

    await act(async () => {
      // Detached, so this cannot even reach the hook — asserted because a
      // quantum in flight during teardown is the realistic case.
      node.deliver(quantum(SAMPLES_PER_FRAME));
    });

    // No socket at all: the quantum never reached the hook, so it never
    // triggered the "first message validated, now connect" path.
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(result.current.state.status).toBe('idle');
  });
});
