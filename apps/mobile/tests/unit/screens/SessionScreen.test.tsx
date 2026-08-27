import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import type { AudioCaptureErrorCode } from '@medauth/audio-wire';

import type { ApiResult, Session, SessionsApi, StartVisitInput } from '../../../src/api/sessions';
import type { AudioCaptureState } from '../../../src/hooks/useAudioCapture';
import type { PatientSource } from '../../../src/session/patientSource';
import { tokenExpiringAt } from '../../support/token';
import {
  END_FAILED_MESSAGE,
  NO_SUBJECT_MESSAGE,
  SessionScreen,
  VISIT_COMPLETED_MESSAGE,
} from '../../../src/screens/SessionScreen';

/**
 * The capture hook is mocked here rather than driven through `expo-audio`.
 *
 * TASK-022 and TASK-026 already test the hook against a fake stream and a fake
 * socket; what this file is responsible for is the screen's reaction to each
 * state the hook can report, which is exactly the boundary being stubbed. The
 * mock is stateful — a real `useState` — so a test can push a new state and have
 * the component re-render the way it would in the app.
 */
const mockCaptureStart = jest.fn<Promise<void>, []>();
const mockCaptureStop = jest.fn<void, []>();
const mockCaptureOptions: { current: { sessionId: string; jwt: string; baseUrl: string } | null } = {
  current: null,
};
const mockCaptureSetters = new Set<(state: AudioCaptureState) => void>();

jest.mock('../../../src/hooks/useAudioCapture', () => {
  // A jest.mock factory is hoisted above the imports, so React has to be pulled
  // in here rather than referenced from module scope.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const react = require('react') as typeof import('react');
  return {
    useAudioCapture: (options: { sessionId: string; jwt: string; baseUrl: string }) => {
      const [state, setState] = react.useState<AudioCaptureState>({ status: 'idle' });
      mockCaptureOptions.current = options;
      react.useEffect(() => {
        mockCaptureSetters.add(setState);
        return () => {
          mockCaptureSetters.delete(setState);
        };
      }, []);
      return { state, start: mockCaptureStart, stop: mockCaptureStop };
    },
  };
});

const RECORDING_LABEL = 'Recording in progress';
const NOW = 1_700_000_000_000;
const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';

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

function token(secondsFromNow: number): string {
  return tokenExpiringAt(NOW, secondsFromNow);
}

const FRESH_TOKEN = token(900);
const NEARLY_EXPIRED_TOKEN = token(10);

const startVisit = jest.fn<Promise<ApiResult<Session>>, [StartVisitInput]>();
const remintToken = jest.fn<Promise<ApiResult<Session>>, [string, string]>();
const endVisit = jest.fn<Promise<ApiResult<void>>, [string]>();
const sessions: SessionsApi = { startVisit, remintToken, endVisit };

const aPatient: PatientSource = async () => ({ patientId: 'patient-1', providerId: PROVIDER_ID });

function ok<T>(value: T): ApiResult<T> {
  return { ok: true, value };
}

async function emitCapture(state: AudioCaptureState): Promise<void> {
  await act(async () => {
    mockCaptureSetters.forEach((set) => set(state));
  });
}

function renderScreen(overrides: Partial<React.ComponentProps<typeof SessionScreen>> = {}) {
  return render(
    <SessionScreen patientSource={aPatient} sessions={sessions} now={() => NOW} {...overrides} />,
  );
}

/** Render, tap "Start visit", and wait for capture to have been asked to start. */
async function startVisitOnScreen(
  overrides: Partial<React.ComponentProps<typeof SessionScreen>> = {},
) {
  const screen = await renderScreen(overrides);
  await fireEvent.press(screen.getByTestId('start-visit'));
  await waitFor(() => expect(mockCaptureStart).toHaveBeenCalled());
  return screen;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockCaptureSetters.clear();
  mockCaptureOptions.current = null;
  mockCaptureStart.mockResolvedValue(undefined);
  // The real `stop()` tears down and returns the hook to idle.
  mockCaptureStop.mockImplementation(() => {
    mockCaptureSetters.forEach((set) => set({ status: 'idle' }));
  });
  startVisit.mockResolvedValue(ok({ sessionId: SESSION_ID, jwt: FRESH_TOKEN }));
  remintToken.mockResolvedValue(ok({ sessionId: SESSION_ID, jwt: FRESH_TOKEN }));
  endVisit.mockResolvedValue(ok(undefined));
});

describe('starting a visit', () => {
  it('refuses to start when nothing can identify the patient', async () => {
    // The seam has no implementation until TASK-025b. Refusing is the honest
    // answer; a hardcoded id would file the encounter against a real stranger.
    const screen = await renderScreen({ patientSource: async () => null });

    await fireEvent.press(screen.getByTestId('start-visit'));

    expect(await screen.findByText(NO_SUBJECT_MESSAGE)).toBeTruthy();
    expect(startVisit).not.toHaveBeenCalled();
    expect(mockCaptureStart).not.toHaveBeenCalled();
  });

  it('creates the encounter and hands the session to capture', async () => {
    const screen = await startVisitOnScreen();

    expect(startVisit).toHaveBeenCalledWith({ patientId: 'patient-1', providerId: PROVIDER_ID });
    expect(mockCaptureOptions.current).toEqual(
      expect.objectContaining({ sessionId: SESSION_ID, jwt: FRESH_TOKEN }),
    );
    expect(screen.getByText('Connecting the microphone…')).toBeTruthy();
    // Creating the encounter is not recording it.
    expect(screen.queryByText(RECORDING_LABEL)).toBeNull();
  });

  it('reports a refused start without pretending a visit began', async () => {
    startVisit.mockResolvedValue({
      ok: false,
      failure: { kind: 'status', status: 503, code: 'redis_unavailable', message: 'No consumer.' },
    });
    const screen = await renderScreen();

    await fireEvent.press(screen.getByTestId('start-visit'));

    expect(await screen.findByTestId('visit-error')).toBeTruthy();
    expect(screen.queryByText(RECORDING_LABEL)).toBeNull();
    expect(mockCaptureStart).not.toHaveBeenCalled();
  });
});

describe('the visit never reaches "in progress" while capture has failed', () => {
  it('blocks the visit and renders the error when the device cannot do 16kHz', async () => {
    const screen = await startVisitOnScreen();

    await emitCapture({
      status: 'error',
      error: {
        code: 'SAMPLE_RATE_UNSUPPORTED',
        message: 'This device captured audio at 44100 Hz; MedAuth AI requires 16000 Hz.',
        detail: { requested: { sampleRate: 16_000, channels: 1 }, actual: { sampleRate: 44_100, channels: 1 } },
      },
    });

    expect(screen.queryByText(RECORDING_LABEL)).toBeNull();
    expect(screen.getByTestId('capture-error')).toBeTruthy();
    expect(screen.getByText(/44100 Hz/)).toBeTruthy();
    // Not retryable on this hardware — offering a retry would invite a loop.
    expect(screen.queryByTestId('retry-capture')).toBeNull();
  });

  it('blocks the visit and offers the settings route when permission is denied', async () => {
    const openSettings = jest.fn();
    const screen = await startVisitOnScreen({ openSettings });

    await emitCapture({
      status: 'error',
      error: {
        code: 'PERMISSION_DENIED',
        message: 'MedAuth AI needs microphone access to record this encounter.',
      },
    });

    expect(screen.queryByText(RECORDING_LABEL)).toBeNull();
    expect(screen.getByText('MedAuth AI needs microphone access to record this encounter.')).toBeTruthy();

    await fireEvent.press(screen.getByTestId('open-settings'));
    expect(openSettings).toHaveBeenCalled();
  });

  it.each(ALL_CODES)('blocks the visit for %s and says so', async (code) => {
    // The rule is over the whole vocabulary, not the codes that happened to get
    // their own bullet: a code with no dedicated branch must still block.
    const screen = await startVisitOnScreen();

    await emitCapture({ status: 'error', error: { code, message: `capture failed: ${code}` } });

    expect(screen.queryByText(RECORDING_LABEL)).toBeNull();
    expect(screen.getByText(`capture failed: ${code}`)).toBeTruthy();
    expect(screen.getByTestId('capture-error')).toBeTruthy();
  });

  it('offers a plain retry for a stream that dropped, and says what was lost', async () => {
    const screen = await startVisitOnScreen();
    await emitCapture({ status: 'streaming' });
    expect(screen.getByText(RECORDING_LABEL)).toBeTruthy();

    await emitCapture({
      status: 'error',
      error: { code: 'STREAM_FAILED', message: 'The audio connection closed unexpectedly.' },
    });

    expect(screen.queryByText(RECORDING_LABEL)).toBeNull();
    // STREAM_FAILED is the one code where part of the encounter did reach the
    // server, so the retry offer must not claim nothing was recorded.
    expect(screen.getByText(/does not recover the audio that was missed/)).toBeTruthy();
    expect(screen.getByTestId('retry-capture')).toBeTruthy();
  });

  it('retries capture without creating a second encounter', async () => {
    const screen = await startVisitOnScreen();
    await emitCapture({
      status: 'error',
      error: { code: 'CAPTURE_TIMED_OUT', message: 'No audio reached MedAuth AI.' },
    });

    await fireEvent.press(screen.getByTestId('retry-capture'));

    await waitFor(() => expect(mockCaptureStart).toHaveBeenCalledTimes(2));
    expect(startVisit).toHaveBeenCalledTimes(1);
  });
});

describe('a token that outlasts the visit is re-minted, never restarted', () => {
  it('refreshes before opening a socket when the held token is near exp', async () => {
    startVisit.mockResolvedValue(ok({ sessionId: SESSION_ID, jwt: NEARLY_EXPIRED_TOKEN }));
    await startVisitOnScreen();

    expect(remintToken).toHaveBeenCalledWith(SESSION_ID, NEARLY_EXPIRED_TOKEN);
    // Capture must run against the fresh token, not the one it was created with.
    expect(mockCaptureOptions.current?.jwt).toBe(FRESH_TOKEN);
    expect(startVisit).toHaveBeenCalledTimes(1);
  });

  it('does not open a socket when the pre-flight refresh fails', async () => {
    // The token was already too close to `exp` to use, so failing to refresh it
    // leaves nothing to open a socket with. Saying so beats starting capture
    // with a credential that is about to be rejected.
    startVisit.mockResolvedValue(ok({ sessionId: SESSION_ID, jwt: NEARLY_EXPIRED_TOKEN }));
    remintToken.mockResolvedValue({
      ok: false,
      failure: { kind: 'network', message: 'unreachable' },
    });
    const screen = await renderScreen();

    await fireEvent.press(screen.getByTestId('start-visit'));

    expect(await screen.findByText(/could not be refreshed/)).toBeTruthy();
    expect(screen.queryByText(RECORDING_LABEL)).toBeNull();
    expect(mockCaptureStart).not.toHaveBeenCalled();
  });

  it('re-mints on AUTH_REJECTED rather than starting a second encounter', async () => {
    const screen = await startVisitOnScreen();
    remintToken.mockResolvedValue(ok({ sessionId: SESSION_ID, jwt: token(900) }));

    await emitCapture({
      status: 'error',
      error: { code: 'AUTH_REJECTED', message: 'The audio connection was refused.' },
    });
    await fireEvent.press(screen.getByTestId('retry-capture'));

    await waitFor(() => expect(remintToken).toHaveBeenCalledWith(SESSION_ID, FRESH_TOKEN));
    // The failure this guards: a second /sessions/start forks one visit into two
    // encounters and nothing errors anywhere along that path.
    expect(startVisit).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(mockCaptureStart).toHaveBeenCalledTimes(2));
  });

  it('treats a 409 from the re-mint as the visit being over', async () => {
    const screen = await startVisitOnScreen();
    remintToken.mockResolvedValue({
      ok: false,
      failure: { kind: 'status', status: 409, code: 'session_completed', message: 'completed' },
    });

    await emitCapture({
      status: 'error',
      error: { code: 'AUTH_REJECTED', message: 'The audio connection was refused.' },
    });
    await fireEvent.press(screen.getByTestId('retry-capture'));

    expect(await screen.findByText(VISIT_COMPLETED_MESSAGE)).toBeTruthy();
    // The only case where a provider is asked to start a new visit.
    expect(screen.getByTestId('start-visit')).toBeTruthy();
  });
});

describe('ending a visit', () => {
  it('moves idle → connecting → recording → ended', async () => {
    const screen = await startVisitOnScreen();
    expect(screen.getByText('Connecting the microphone…')).toBeTruthy();

    await emitCapture({ status: 'streaming' });
    expect(screen.getByText(RECORDING_LABEL)).toBeTruthy();

    await fireEvent.press(screen.getByTestId('end-visit'));

    await waitFor(() => expect(screen.getByText('Visit ended.')).toBeTruthy());
    expect(endVisit).toHaveBeenCalledWith(SESSION_ID);
    // Capture stops and its buffered audio is dropped; nothing keeps recording
    // while the encounter is being closed.
    expect(mockCaptureStop).toHaveBeenCalled();
  });

  it('reports a failed end and lets the provider try again', async () => {
    endVisit.mockResolvedValueOnce({
      ok: false,
      failure: { kind: 'network', message: 'unreachable' },
    });
    const screen = await startVisitOnScreen();
    await emitCapture({ status: 'streaming' });

    await fireEvent.press(screen.getByTestId('end-visit'));
    expect(await screen.findByText(END_FAILED_MESSAGE)).toBeTruthy();

    await fireEvent.press(screen.getByTestId('retry-end'));

    await waitFor(() => expect(endVisit).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Visit ended.')).toBeTruthy();
  });

  it('can end a visit that failed to record', async () => {
    const screen = await startVisitOnScreen();
    await emitCapture({
      status: 'error',
      error: { code: 'CAPTURE_FAILED', message: 'The microphone could not be started.' },
    });

    await fireEvent.press(screen.getByTestId('end-visit'));

    await waitFor(() => expect(endVisit).toHaveBeenCalledWith(SESSION_ID));
  });
});
