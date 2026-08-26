import { act, renderHook, waitFor } from '@testing-library/react-native';

import { CHUNK_BYTES, MAX_PENDING_BYTES } from '@medauth/audio-wire';

import { FIRST_AUDIO_TIMEOUT_MS, useAudioCapture } from '../../../src/hooks/useAudioCapture';

/**
 * Host byte order, as the hook sees it.
 *
 * Stubbed through `jest.mock` rather than `jest.spyOn` on the module namespace:
 * `@medauth/audio-wire` re-exports through a barrel, whose properties are
 * getters that a spy cannot redefine. Everything else in the package is the
 * real implementation — this is the one value a test machine cannot vary.
 */
const mockLittleEndian = { current: true };

jest.mock('@medauth/audio-wire', () => ({
  ...jest.requireActual('@medauth/audio-wire'),
  isLittleEndian: () => mockLittleEndian.current,
}));

const mockStreamStart = jest.fn<Promise<void>, []>();
const mockStreamStop = jest.fn<void, []>();
const mockRequestPermission = jest.fn<Promise<{ granted: boolean }>, []>();
const mockOnBufferRef: { current: OnBufferCallback | null } = { current: null };
const mockStreamOptionsRef: { current: Record<string, unknown> | null } = { current: null };
/**
 * What `AudioStream` reports it is actually delivering once started. Real
 * hardware fills these in; the hook compares them before any buffer arrives.
 */
const mockReportedFormat = { sampleRate: 16_000, channels: 1 };

interface CapturedBuffer {
  data: ArrayBuffer;
  sampleRate: number;
  channels: number;
}

type OnBufferCallback = (captured: CapturedBuffer) => void;

jest.mock('expo-audio', () => ({
  useAudioStream: (options: Record<string, unknown>) => {
    mockStreamOptionsRef.current = options;
    mockOnBufferRef.current = options.onBuffer as OnBufferCallback;
    return {
      stream: {
        start: mockStreamStart,
        stop: mockStreamStop,
        get sampleRate() {
          return mockReportedFormat.sampleRate;
        },
        get channels() {
          return mockReportedFormat.channels;
        },
      },
      isStreaming: false,
    };
  },
  requestRecordingPermissionsAsync: () => mockRequestPermission(),
}));

/** Stand-in for React Native's WebSocket, which takes a third options argument. */
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
    readonly protocols: string[] | undefined,
    readonly options: { headers: Record<string, string> },
  ) {
    FakeWebSocket.instances.push(this);
  }

  send(data: Uint8Array): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  /** Drive the handshake completing. */
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  /** Drive a close, as a refused upgrade would produce. */
  fireClose(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

const OPTIONS = {
  sessionId: '11111111-1111-4111-8111-111111111111',
  jwt: 'header.payload.signature',
  baseUrl: 'wss://audio.example',
};

function pcm(bytes: number, fill = 1): ArrayBuffer {
  const view = new Uint8Array(bytes);
  view.fill(fill);
  return view.buffer;
}

function buffer(overrides: Partial<CapturedBuffer> = {}): CapturedBuffer {
  return { data: pcm(CHUNK_BYTES), sampleRate: 16_000, channels: 1, ...overrides };
}

/** The single socket the hook opened, or undefined if it opened none. */
function socket(): FakeWebSocket | undefined {
  return FakeWebSocket.instances[0];
}

async function deliver(buf: CapturedBuffer): Promise<void> {
  await act(async () => {
    mockOnBufferRef.current?.(buf);
  });
}

beforeEach(() => {
  jest.restoreAllMocks();
  jest.clearAllMocks();
  FakeWebSocket.instances = [];
  mockOnBufferRef.current = null;
  mockStreamStart.mockResolvedValue(undefined);
  mockStreamStop.mockReturnValue(undefined);
  mockReportedFormat.sampleRate = 16_000;
  mockReportedFormat.channels = 1;
  mockRequestPermission.mockResolvedValue({ granted: true });
  (globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket;
});

afterEach(() => {
  // Only the deadline tests install fake timers; restoring unconditionally keeps
  // a failure inside one of them from leaking a frozen clock into the next test.
  jest.useRealTimers();
});

describe('useAudioCapture — requested format', () => {
  it('asks the microphone for 16kHz mono int16', async () => {
    await renderHook(() => useAudioCapture(OPTIONS));

    expect(mockStreamOptionsRef.current).toMatchObject({
      sampleRate: 16_000,
      channels: 1,
      encoding: 'int16',
    });
  });
});

describe('useAudioCapture — the socket does not open before the format is validated', () => {
  it('starts the stream but opens no socket until a buffer arrives', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });

    expect(mockRequestPermission).toHaveBeenCalledTimes(1);
    expect(mockStreamStart).toHaveBeenCalledTimes(1);
    // The reported rate matched, but nothing has been captured yet — the
    // socket waits for a delivered buffer, so nothing can be sent before the
    // audio that would be sent has been inspected.
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(result.current.state.status).toBe('starting');
  });

  it('opens the socket once the first buffer matches', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await deliver(buffer());

    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(socket()?.url).toBe(`wss://audio.example/ws/audio/${OPTIONS.sessionId}`);
  });
});

describe('useAudioCapture — the rate the stream reports is checked first', () => {
  it('fails immediately after start, without waiting for a buffer', async () => {
    mockReportedFormat.sampleRate = 44_100;
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });

    // No buffer has been delivered. Waiting for one would leave the failure
    // invisible on a device that produces none at all.
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'SAMPLE_RATE_UNSUPPORTED' },
    });
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('treats an unreported rate as not-yet-known rather than a mismatch', async () => {
    // Zero means the native side has not filled it in; the first buffer settles
    // it. Failing here would refuse capture on a device that works.
    mockReportedFormat.sampleRate = 0;
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });

    expect(result.current.state.status).toBe('starting');

    await deliver(buffer());
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

describe('useAudioCapture — a format mismatch fails closed', () => {
  it('reports SAMPLE_RATE_UNSUPPORTED and never opens a socket', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await deliver(buffer({ sampleRate: 44_100 }));

    // This is the whole point of the ordering: Transcribe would have hung on
    // 44.1kHz rather than erroring, so no audio may reach it at all.
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(result.current.state).toEqual({
      status: 'error',
      error: expect.objectContaining({
        code: 'SAMPLE_RATE_UNSUPPORTED',
        detail: { requested: { sampleRate: 16_000, channels: 1 }, actual: { sampleRate: 44_100, channels: 1 } },
      }),
    });
  });

  it('stops the microphone on a mismatch rather than leaving it open', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await deliver(buffer({ sampleRate: 8_000 }));

    expect(mockStreamStop).toHaveBeenCalled();
  });

  it('reports CHANNELS_UNSUPPORTED for a stereo capture', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await deliver(buffer({ channels: 2 }));

    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'CHANNELS_UNSUPPORTED' },
    });
  });

  it('does not throw the mismatch, so an error boundary cannot swallow it', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    // Delivering a mismatched buffer must not reject or throw — the failure is
    // returned in state, which is what makes it survivable and visible.
    await expect(deliver(buffer({ sampleRate: 48_000 }))).resolves.toBeUndefined();
    expect(result.current.state.status).toBe('error');
  });
});

describe('useAudioCapture — chunking', () => {
  async function streaming() {
    const rendered = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await rendered.result.current.start();
    });
    await deliver(buffer({ data: pcm(0) }));
    await act(async () => {
      socket()?.open();
    });
    return rendered;
  }

  it('sends exactly one 8000-byte frame per 250ms of audio', async () => {
    const { result } = await streaming();

    await deliver(buffer({ data: pcm(CHUNK_BYTES) }));

    expect(socket()?.sent).toHaveLength(1);
    expect(socket()?.sent[0]).toHaveLength(CHUNK_BYTES);
    expect(result.current.state.status).toBe('streaming');
  });

  it('holds a partial tail back rather than sending it early', async () => {
    await streaming();

    await deliver(buffer({ data: pcm(CHUNK_BYTES - 1) }));

    expect(socket()?.sent).toHaveLength(0);
  });

  it('re-chunks many small native buffers into whole frames', async () => {
    await streaming();

    for (let i = 0; i < 8; i += 1) {
      await deliver(buffer({ data: pcm(CHUNK_BYTES / 4) }));
    }

    expect(socket()?.sent).toHaveLength(2);
    expect(socket()?.sent.every((frame) => frame.length === CHUNK_BYTES)).toBe(true);
  });

  it('flushes audio captured while the socket was still connecting', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    // The first buffer opens the socket; the handshake has not completed yet.
    await deliver(buffer({ data: pcm(CHUNK_BYTES) }));
    expect(socket()?.sent).toHaveLength(0);

    await act(async () => {
      socket()?.open();
    });

    expect(socket()?.sent).toHaveLength(1);
  });

  it('reports SEND_BACKLOG_EXCEEDED, not STREAM_FAILED, when the socket never opens', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await deliver(buffer({ data: pcm(CHUNK_BYTES) }));
    for (let i = 0; i < MAX_PENDING_BYTES / CHUNK_BYTES; i += 1) {
      await deliver(buffer({ data: pcm(CHUNK_BYTES) }));
    }

    // Nothing was transmitted and the encounter never started, which is a
    // different thing to tell a provider than a connection that dropped
    // partway through — so it does not share STREAM_FAILED's code.
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'SEND_BACKLOG_EXCEEDED' },
    });
  });
});

describe('useAudioCapture — the session token', () => {
  it('travels in the Authorization header', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await deliver(buffer());

    // React Native can set a header, unlike the browser — see CLAUDE.md, "How
    // the JWT reaches a WebSocket endpoint".
    expect(socket()?.options.headers.Authorization).toBe(`Bearer ${OPTIONS.jwt}`);
  });

  it('never appears in the URL', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await deliver(buffer());

    // A query-string credential is the one carrier guaranteed to be logged by
    // intermediaries.
    expect(socket()?.url).not.toContain(OPTIONS.jwt);
    expect(socket()?.url).not.toContain('?');
  });

  it('never appears in an error message', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await deliver(buffer());
    await act(async () => {
      socket()?.fireClose();
    });

    const { state } = result.current;
    expect(state.status).toBe('error');
    if (state.status === 'error') {
      expect(state.error.message).not.toContain(OPTIONS.jwt);
    }
  });
});

describe('useAudioCapture — connection failures', () => {
  it('treats a socket that never opened as a refused token', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer());

    await act(async () => {
      socket()?.fireClose();
    });

    // TASK-020 rejects before the handshake completes, so there is no 4401
    // close frame to read — a failed upgrade is the signal.
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'AUTH_REJECTED' },
    });
  });

  it('reports an onerror before open as a refused token too', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer());

    await act(async () => {
      socket()?.onerror?.();
    });

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'AUTH_REJECTED' },
    });
  });

  it('distinguishes a socket that opened and then dropped', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer());
    await act(async () => {
      socket()?.open();
    });

    await act(async () => {
      socket()?.fireClose();
    });

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'STREAM_FAILED' },
    });
  });

  it('reports CAPTURE_FAILED without surfacing the underlying error', async () => {
    mockStreamStart.mockRejectedValue(new Error('/dev/audio0: device busy'));
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });

    const { state } = result.current;
    expect(state).toMatchObject({ status: 'error', error: { code: 'CAPTURE_FAILED' } });
    if (state.status === 'error') {
      expect(state.error.message).not.toContain('/dev/audio0');
    }
  });
});

describe('useAudioCapture — a socket event after stop is ignored', () => {
  it('does not report a close that arrives after the caller stopped', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer());
    await act(async () => {
      socket()?.open();
    });
    await act(async () => {
      result.current.stop();
    });

    await act(async () => {
      socket()?.fireClose();
    });

    // Closing is what stop asked for. Reporting it as a failure would put a
    // stopped session into an error state a screen would then have to explain.
    expect(result.current.state.status).toBe('idle');
  });

  it('does not report an error once the socket has opened', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer());
    await act(async () => {
      socket()?.open();
    });

    await act(async () => {
      socket()?.onerror?.();
    });

    // An error after a successful open is not an auth problem; the close
    // handler reports it with the right code if the connection actually drops.
    expect(result.current.state.status).toBe('streaming');
  });
});

describe('useAudioCapture — endianness', () => {
  afterEach(() => {
    mockLittleEndian.current = true;
  });

  it('refuses to capture on a big-endian platform', async () => {
    // Transcribe requires little-endian int16. Both targets are little-endian,
    // which is what makes this worth a guard: the failure would otherwise be
    // noise reaching the transcriber rather than an error anyone sees.
    mockLittleEndian.current = false;
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });

    expect(mockRequestPermission).not.toHaveBeenCalled();
    expect(mockStreamStart).not.toHaveBeenCalled();
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'ENDIANNESS_UNSUPPORTED' },
    });
  });
});

describe('useAudioCapture — permission', () => {
  it('reports PERMISSION_DENIED and starts nothing', async () => {
    mockRequestPermission.mockResolvedValue({ granted: false });
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });

    expect(mockStreamStart).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'PERMISSION_DENIED' },
    });
  });
});

describe('useAudioCapture — stopping', () => {
  it('stops the stream, closes the socket and returns to idle', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer());
    await act(async () => {
      socket()?.open();
    });

    await act(async () => {
      result.current.stop();
    });

    await waitFor(() => expect(result.current.state.status).toBe('idle'));
    expect(mockStreamStop).toHaveBeenCalled();
    expect(socket()?.closed).toBe(true);
  });

  it('drops buffered audio, so a partial frame cannot survive into the next visit', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer({ data: pcm(CHUNK_BYTES / 2) }));
    await act(async () => {
      socket()?.open();
    });

    await act(async () => {
      result.current.stop();
    });
    const sentBeforeRestart = socket()?.sent.length ?? 0;

    // Half a frame was held at stop. If it were still held, this half would
    // complete it and send audio from the previous encounter.
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer({ data: pcm(CHUNK_BYTES / 2) }));

    expect(FakeWebSocket.instances[1]?.sent ?? []).toHaveLength(0);
    expect(sentBeforeRestart).toBe(0);
  });

  it('ignores buffers that arrive after stop', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer());
    await act(async () => {
      socket()?.open();
    });
    await act(async () => {
      result.current.stop();
    });
    // The setup already streamed one frame; what matters is that no *further*
    // frame is sent, not that none ever was.
    const sentAtStop = socket()?.sent.length ?? 0;

    await deliver(buffer({ data: pcm(CHUNK_BYTES) }));

    expect(socket()?.sent).toHaveLength(sentAtStop);
  });

  it('releases the microphone on unmount', async () => {
    const { result, unmount } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });
    await deliver(buffer());
    mockStreamStop.mockClear();

    await unmount();

    expect(mockStreamStop).toHaveBeenCalled();
  });

  it('is a no-op when start is called twice', async () => {
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });
    await act(async () => {
      await result.current.start();
    });

    expect(mockStreamStart).toHaveBeenCalledTimes(1);
  });
});

/** Run the fake clock forward, letting React settle whatever the timer caused. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

describe('useAudioCapture — the wait for the first buffer is bounded', () => {
  it('reports CAPTURE_TIMED_OUT when no buffer ever arrives', async () => {
    jest.useFakeTimers();
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });

    // Permission granted, the stream started, the rate it reported matched —
    // every upstream step said success and no audio came.
    expect(result.current.state.status).toBe('starting');

    await advance(FIRST_AUDIO_TIMEOUT_MS);

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'CAPTURE_TIMED_OUT' },
    });
    // Nothing was captured, so nothing may have been transmitted.
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(mockStreamStop).toHaveBeenCalled();
  });

  it('times out even when the stream never reports a rate at all', async () => {
    // Zero means the native side has not filled the rate in. `start()` skips the
    // reported-format check entirely in that case, which is exactly the path
    // that had nothing bounding it: no mismatch to catch, and no buffer coming.
    mockReportedFormat.sampleRate = 0;
    jest.useFakeTimers();
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });
    await advance(FIRST_AUDIO_TIMEOUT_MS);

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'CAPTURE_TIMED_OUT' },
    });
  });

  it('cancels the deadline once a buffer arrives', async () => {
    jest.useFakeTimers();
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await deliver(buffer());
    await act(async () => {
      socket()?.open();
    });
    await advance(FIRST_AUDIO_TIMEOUT_MS * 3);

    // An encounter longer than the start-up deadline must not be torn down by it.
    expect(result.current.state.status).toBe('streaming');
    expect(socket()?.closed).toBe(false);
  });

  it('cancels the deadline on stop, so a stopped hook cannot fail late', async () => {
    jest.useFakeTimers();
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));
    await act(async () => {
      await result.current.start();
    });

    await act(async () => {
      result.current.stop();
    });
    await advance(FIRST_AUDIO_TIMEOUT_MS * 3);

    expect(result.current.state.status).toBe('idle');
  });

  it('does not overwrite a format failure with a late timeout', async () => {
    // The reported-rate check fails inside `start()`, before the deadline is
    // armed. A timeout landing on top of it would replace an accurate message —
    // this hardware cannot capture what Transcribe needs, do not retry — with
    // one inviting the retry that will fail the same way.
    mockReportedFormat.sampleRate = 44_100;
    jest.useFakeTimers();
    const { result } = await renderHook(() => useAudioCapture(OPTIONS));

    await act(async () => {
      await result.current.start();
    });
    await advance(FIRST_AUDIO_TIMEOUT_MS * 3);

    expect(result.current.state).toMatchObject({
      status: 'error',
      error: { code: 'SAMPLE_RATE_UNSUPPORTED' },
    });
  });
});
