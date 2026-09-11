import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { PatientSource, VisitSubject } from '@medauth/fhir-client';

import type { ApiResult, Session, SessionsApi } from '../../../src/api/sessions';
import {
  END_FAILED_MESSAGE,
  NO_SUBJECT_MESSAGE,
  RECORDING_LABEL,
  SessionScreen,
  VISIT_COMPLETED_MESSAGE,
} from '../../../src/screens/SessionScreen';
import { FakeWebSocket, installFakeWebSocket, tokenExpiringAt } from '../../support/transcript';

/**
 * The session screen's state transitions (TASK-070's required test).
 *
 * The capture hook is mocked rather than driven through a real `AudioContext`:
 * jsdom has no audio graph, `useAudioCapture` has its own suite against a fake
 * one, and what is under test here is the screen's transitions — start, active,
 * end — and the rule that a visit is never shown as recording unless capture
 * says it is streaming.
 *
 * The nudge and transcript sockets are the fake WebSocket, so mounting the
 * active view does not reach the network.
 */

const SESSION_ID = '11111111-1111-4111-8111-111111111111';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';
const NOW = 1_700_000_000_000;
const FRESH = tokenExpiringAt(NOW, 900);
const STALE = tokenExpiringAt(NOW, 10);

const SUBJECT: VisitSubject = {
  patientId: 'patient-1',
  providerId: PROVIDER_ID,
  ehrEncounterId: 'Encounter/7',
  launchId: 'launch-7',
};

const now = () => NOW;

/**
 * The capture hook, replaced with a handle the tests drive.
 *
 * `start` and `stop` are recorded rather than simulated, and the state is set
 * from the test, which is what lets a single test put the screen in a capture
 * state the real hook only reaches through a device.
 */
const capture = vi.hoisted(() => ({
  state: { status: 'idle' } as { status: string; error?: { code: string; message: string } },
  start: vi.fn(async () => {}),
  stop: vi.fn(),
  setState: (next: { status: string; error?: { code: string; message: string } }) => {
    capture.state = next;
  },
}));

vi.mock('../../../src/hooks/useAudioCapture', () => ({
  useAudioCapture: () => ({ state: capture.state, start: capture.start, stop: capture.stop }),
}));

function sessionsThatWork(overrides: Partial<SessionsApi> = {}): SessionsApi {
  return {
    startVisit: vi.fn(
      async (): Promise<ApiResult<Session>> => ({
        ok: true,
        value: { sessionId: SESSION_ID, jwt: FRESH },
      }),
    ),
    remintToken: vi.fn(
      async (): Promise<ApiResult<Session>> => ({
        ok: true,
        value: { sessionId: SESSION_ID, jwt: FRESH },
      }),
    ),
    endVisit: vi.fn(async () => ({ ok: true as const, value: undefined })),
    ...overrides,
  } as unknown as SessionsApi;
}

const source: PatientSource = async () => SUBJECT;

function renderScreen(sessions: SessionsApi, patientSource: PatientSource = source) {
  const onCompleted = vi.fn();
  const element = () => (
    <SessionScreen
      patientSource={patientSource}
      sessions={sessions}
      onCompleted={onCompleted}
      audioBaseUrl="wss://audio.example"
      now={now}
    />
  );
  const view = render(element());

  /**
   * Re-render after moving the mocked capture state.
   *
   * The mock reads a plain object, so changing it is invisible to React until
   * something renders. Tests used to force that by clicking "end visit", which
   * meant each of them was really asserting about an ended visit — one named for
   * the recording label passed while never rendering a recording phase at all.
   *
   * A *fresh* element each time, not the one already rendered: React bails out
   * of re-rendering when handed a referentially identical element, so reusing it
   * reproduces the same silent no-op in a different disguise.
   */
  return { onCompleted, refresh: () => view.rerender(element()) };
}

let restore: () => void;

beforeEach(() => {
  restore = installFakeWebSocket();
  capture.setState({ status: 'idle' });
  capture.start.mockClear();
  capture.stop.mockClear();
});

afterEach(() => {
  restore();
});

describe('starting a visit', () => {
  it('starts idle, offering only a start action', () => {
    renderScreen(sessionsThatWork());

    expect(screen.getByTestId('start-visit')).toBeInTheDocument();
    expect(screen.queryByTestId('end-visit')).not.toBeInTheDocument();
  });

  /**
   * The chart entry and the launch travel together. Either alone leaves the
   * encounter's payer columns NULL, which the policy dispatcher then reports per
   * procedure — so a visit started from a chart must carry both.
   */
  it('sends the patient, provider, chart entry and launch', async () => {
    const sessions = sessionsThatWork();
    renderScreen(sessions);

    fireEvent.click(screen.getByTestId('start-visit'));

    await waitFor(() =>
      expect(sessions.startVisit).toHaveBeenCalledWith({
        patientId: 'patient-1',
        providerId: PROVIDER_ID,
        ehrEncounterId: 'Encounter/7',
        launchId: 'launch-7',
      }),
    );
  });

  it('sends no chart entry for a standalone launch', async () => {
    const sessions = sessionsThatWork();
    renderScreen(sessions, async () => ({
      patientId: 'patient-1',
      providerId: PROVIDER_ID,
      launchId: 'launch-7',
    }));

    fireEvent.click(screen.getByTestId('start-visit'));

    await waitFor(() => expect(sessions.startVisit).toHaveBeenCalled());
    const body = (sessions.startVisit as ReturnType<typeof vi.fn>).mock.calls[0]?.[0] as object;
    expect(body).not.toHaveProperty('ehrEncounterId');
  });

  /**
   * Refusing rather than inventing a subject. A hardcoded or guessed patient is
   * indistinguishable from a real one at runtime, and the encounter, note and
   * prior-auth bundle would all be filed against the wrong person silently.
   */
  it('refuses to start when the subject cannot be resolved', async () => {
    const sessions = sessionsThatWork();
    renderScreen(sessions, async () => null);

    fireEvent.click(screen.getByTestId('start-visit'));

    await waitFor(() => expect(screen.getByTestId('visit-failed')).toHaveTextContent(NO_SUBJECT_MESSAGE));
    expect(sessions.startVisit).not.toHaveBeenCalled();
  });

  it('reports a refused start with the service’s reason', async () => {
    const sessions = sessionsThatWork({
      startVisit: vi.fn(async () => ({
        ok: false as const,
        failure: { kind: 'status' as const, status: 422, code: 'bad', message: 'Unknown patient.' },
      })),
    } as Partial<SessionsApi>);
    renderScreen(sessions);

    fireEvent.click(screen.getByTestId('start-visit'));

    await waitFor(() => expect(screen.getByTestId('visit-failed')).toHaveTextContent('Unknown patient.'));
  });
});

describe('the active visit', () => {
  async function startVisit(sessions: SessionsApi) {
    const handles = renderScreen(sessions);
    fireEvent.click(screen.getByTestId('start-visit'));
    await waitFor(() => expect(sessions.startVisit).toHaveBeenCalled());
    return handles;
  }

  /**
   * The central requirement: an open encounter is not a recording one. Only the
   * capture hook reporting `streaming` produces the recording label.
   */
  it('shows connecting, not recording, until capture is streaming', async () => {
    await startVisit(sessionsThatWork());

    await waitFor(() => expect(screen.getByTestId('end-visit')).toBeInTheDocument());
    expect(screen.queryByTestId('recording')).not.toBeInTheDocument();
  });

  it('shows recording once capture is streaming', async () => {
    const sessions = sessionsThatWork();
    const { refresh } = await startVisit(sessions);
    await waitFor(() => expect(capture.start).toHaveBeenCalled());

    act(() => capture.setState({ status: 'streaming' }));
    act(() => refresh());

    expect(screen.getByTestId('recording')).toHaveTextContent(RECORDING_LABEL);
  });

  it('mounts the transcript pane and the checklist while the encounter is open', async () => {
    await startVisit(sessionsThatWork());

    await waitFor(() => expect(screen.getByLabelText('Live transcript')).toBeInTheDocument());
    expect(screen.getByLabelText('Flagged procedures')).toBeInTheDocument();
    expect(screen.getByTestId('checklist-empty')).toBeInTheDocument();
  });

  /**
   * The audio socket, the transcript socket and the nudge socket are three
   * independent connections. Hiding the transcript because the microphone failed
   * would hide the evidence of what actually reached the server.
   */
  it('keeps the transcript pane mounted when capture fails', async () => {
    const { refresh } = await startVisit(sessionsThatWork());
    await waitFor(() => expect(capture.start).toHaveBeenCalled());

    act(() =>
      capture.setState({
        status: 'error',
        error: { code: 'CAPTURE_FAILED', message: 'The microphone stopped.' },
      }),
    );
    act(() => refresh());

    expect(screen.getByTestId('capture-failed')).toBeInTheDocument();
    expect(screen.getByLabelText('Live transcript')).toBeInTheDocument();
  });
});

describe('a token near expiry', () => {
  /**
   * Re-minted through `POST /sessions/{id}/token`, never by calling
   * `/sessions/start` again — which would fork one visit into two encounters,
   * splitting the transcript, the note and the nudge dedup, with nothing
   * erroring anywhere along the way.
   */
  it('refreshes before capture starts and never restarts the visit', async () => {
    const sessions = sessionsThatWork({
      startVisit: vi.fn(async () => ({
        ok: true as const,
        value: { sessionId: SESSION_ID, jwt: STALE },
      })),
    } as Partial<SessionsApi>);
    renderScreen(sessions);

    fireEvent.click(screen.getByTestId('start-visit'));

    await waitFor(() => expect(sessions.remintToken).toHaveBeenCalledWith(SESSION_ID, STALE));
    expect(sessions.startVisit).toHaveBeenCalledTimes(1);
  });

  it('ends the visit when the re-mint is refused with a 409', async () => {
    const sessions = sessionsThatWork({
      startVisit: vi.fn(async () => ({
        ok: true as const,
        value: { sessionId: SESSION_ID, jwt: STALE },
      })),
      remintToken: vi.fn(async () => ({
        ok: false as const,
        failure: {
          kind: 'status' as const,
          status: 409,
          code: 'session_completed',
          message: 'over',
        },
      })),
    } as Partial<SessionsApi>);
    renderScreen(sessions);

    fireEvent.click(screen.getByTestId('start-visit'));

    await waitFor(() =>
      expect(screen.getByTestId('visit-failed')).toHaveTextContent(VISIT_COMPLETED_MESSAGE),
    );
  });
});

describe('ending a visit', () => {
  it('stops capture before closing the encounter', async () => {
    const sessions = sessionsThatWork();
    renderScreen(sessions);
    fireEvent.click(screen.getByTestId('start-visit'));
    await waitFor(() => expect(screen.getByTestId('end-visit')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('end-visit'));

    await waitFor(() => expect(capture.stop).toHaveBeenCalled());
    await waitFor(() => expect(sessions.endVisit).toHaveBeenCalledWith(SESSION_ID));
  });

  it('hands the completed session on, for the note review screen', async () => {
    const sessions = sessionsThatWork();
    const { onCompleted } = renderScreen(sessions);
    fireEvent.click(screen.getByTestId('start-visit'));
    await waitFor(() => expect(screen.getByTestId('end-visit')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('end-visit'));
    await waitFor(() => expect(screen.getByTestId('review-note')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('review-note'));

    expect(onCompleted).toHaveBeenCalledWith({ sessionId: SESSION_ID, jwt: FRESH });
  });

  /**
   * Recording has stopped but the encounter is still open server-side, which is
   * not the same as a completed visit: the provider is offered the end action
   * again rather than being told the visit is over.
   */
  it('offers another attempt when the encounter could not be closed', async () => {
    const sessions = sessionsThatWork({
      endVisit: vi.fn(async () => ({
        ok: false as const,
        failure: { kind: 'network' as const, message: 'unreachable' },
      })),
    } as Partial<SessionsApi>);
    renderScreen(sessions);
    fireEvent.click(screen.getByTestId('start-visit'));
    await waitFor(() => expect(screen.getByTestId('end-visit')).toBeInTheDocument());

    fireEvent.click(screen.getByTestId('end-visit'));

    await waitFor(() =>
      expect(screen.getByTestId('visit-failed')).toHaveTextContent(END_FAILED_MESSAGE),
    );
    expect(screen.getByTestId('end-visit')).toBeInTheDocument();
  });

  it('opens no socket once the visit is over', async () => {
    const sessions = sessionsThatWork();
    renderScreen(sessions);
    fireEvent.click(screen.getByTestId('start-visit'));
    await waitFor(() => expect(screen.getByTestId('end-visit')).toBeInTheDocument());
    const during = FakeWebSocket.instances.length;

    fireEvent.click(screen.getByTestId('end-visit'));
    await waitFor(() => expect(screen.getByTestId('visit-ended')).toBeInTheDocument());

    expect(FakeWebSocket.instances.length).toBe(during);
  });
});

describe('a capture failure', () => {
  async function failCapture(code: string, message: string) {
    const sessions = sessionsThatWork();
    const { refresh } = renderScreen(sessions);
    fireEvent.click(screen.getByTestId('start-visit'));
    await waitFor(() => expect(capture.start).toHaveBeenCalled());

    act(() => capture.setState({ status: 'error', error: { code, message } }));
    act(() => refresh());
  }

  it('never shows a visit as recording while capture has failed', async () => {
    await failCapture('CAPTURE_FAILED', 'The microphone stopped.');

    expect(screen.getByTestId('capture-failed')).toHaveTextContent('The microphone stopped.');
    expect(screen.queryByText(RECORDING_LABEL)).not.toBeInTheDocument();
  });

  it('offers a retry for a failure a second attempt could clear', async () => {
    await failCapture('CAPTURE_FAILED', 'The microphone stopped.');

    expect(screen.getByTestId('retry-capture')).toBeInTheDocument();
  });

  /**
   * Retrying hardware that cannot capture 16kHz mono produces a loop that will
   * never succeed, so no retry is offered — the provider is told to use a
   * different browser or device instead.
   */
  it('offers no retry for a failure that cannot clear', async () => {
    await failCapture('SAMPLE_RATE_UNSUPPORTED', 'This device captures at 44.1kHz.');

    expect(screen.queryByTestId('retry-capture')).not.toBeInTheDocument();
    expect(screen.getByText(/use a different browser or device/)).toBeInTheDocument();
  });

  /**
   * A refused token is refreshed before the retry rather than retried as-is —
   * the same rule the sockets follow, reached from the capture side.
   */
  it('re-mints before retrying a refused token', async () => {
    const sessions = sessionsThatWork();
    const { refresh } = renderScreen(sessions);
    fireEvent.click(screen.getByTestId('start-visit'));
    await waitFor(() => expect(capture.start).toHaveBeenCalled());

    act(() =>
      capture.setState({
        status: 'error',
        error: { code: 'AUTH_REJECTED', message: 'The token was refused.' },
      }),
    );
    act(() => refresh());
    fireEvent.click(screen.getByTestId('retry-capture'));

    await waitFor(() => expect(sessions.remintToken).toHaveBeenCalledWith(SESSION_ID, FRESH));
    expect(sessions.startVisit).toHaveBeenCalledTimes(1);
  });
});
