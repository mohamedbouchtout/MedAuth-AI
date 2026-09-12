/**
 * The session screen — start a visit, record it, end it (TASK-070).
 *
 * It calls `POST /sessions/start` (TASK-006), hands the session id and token to
 * `useAudioCapture` (TASK-023), shows the live transcript (TASK-041d) and the
 * nudge overlay (TASK-042) while the encounter runs, and calls
 * `POST /sessions/{id}/end` when the provider is done.
 *
 * Three things here are requirements rather than presentation choices:
 *
 * **A visit is never shown as in progress while capture is in an error state.**
 * The mapping that guarantees it is `visitPhase`, which produces `recording`
 * only from the hook's `streaming` state; this component renders that phase and
 * does not second-guess it. Every code in the shared vocabulary blocks the
 * visit, including any added later — what varies is only what the provider is
 * offered next, which `recoveryFor` decides.
 *
 * **A visit that outlasts its token is refreshed, never restarted.** Both the
 * proactive path (a token near `exp` before a socket is opened) and the reactive
 * one (`AUTH_REJECTED` from a socket that failed to open) go through
 * `POST /sessions/{id}/token` (TASK-006b). `POST /sessions/start` is called in
 * exactly one place in this file — the "start visit" action — because calling it
 * for a token forks one visit into two encounters with nothing erroring
 * anywhere. See CLAUDE.md, "A visit outlasting the token re-mints", which
 * settles this for both session screens; it is not re-derived here.
 *
 * **An empty transcript is never shown as silence.** That distinction lives in
 * `TranscriptPane`, which is always rendered while the encounter is open —
 * including while capture is still connecting, since the two sockets are
 * independent and a provider whose transcript is down needs to know that rather
 * than reading a blank pane as a quiet room.
 *
 * Nothing here logs: the transcript is PHI, the nudges are PHI, and the token is
 * a credential.
 */

import { isNearExpiry } from '@medauth/session-client';
import type { Nudge } from '@medauth/nudge-client';
import { useCallback, useEffect, useRef, useState } from 'react';

import type { PatientSource, VisitSubject } from '@medauth/fhir-client';

import { sessionsApi, type ApiFailure, type Session, type SessionsApi } from '../api/sessions';
import { NudgeOverlay } from '../components/NudgeOverlay';
import { ProcedureChecklist } from '../components/ProcedureChecklist';
import { TranscriptPane } from '../components/TranscriptPane';
import { AUDIO_INGESTION_WS_URL } from '../config';
import { useAudioCapture } from '../hooks/useAudioCapture';
import { useTranscriptStream } from '../hooks/useTranscriptStream';
import type { CompletedVisit } from '../session/completedVisit';
import { recoveryFor } from '../session/recovery';
import { visitPhase, type SessionStatus } from '../session/visitPhase';

/**
 * Shown when the injected source resolves to nothing.
 *
 * The patient and provider are decided before this screen is reached — the
 * patient picker does that, and reports its own failures with its own messages.
 * So this is the last-resort case rather than the ordinary one, and it still
 * refuses rather than inventing a subject.
 */
export const NO_SUBJECT_MESSAGE =
  'MedAuth AI cannot start a visit: the patient and provider for it are not known.';

export const VISIT_COMPLETED_MESSAGE =
  'This visit has already been completed and cannot be reopened. Start a new visit.';

export const END_FAILED_MESSAGE =
  'Recording has stopped, but the visit could not be closed. Try ending it again.';

export const RECORDING_LABEL = 'Recording in progress';

/** Guidance the error's own message does not carry, keyed by what to do next. */
const RECOVERY_GUIDANCE = {
  retry: 'Nothing was recorded. Try again.',
  permission:
    'Allow microphone access for this site in your browser, then try again. The permission prompt is usually reachable from the icon at the left of the address bar.',
  remint: 'The session token was refused. Trying again refreshes it first.',
  unsupported:
    'This browser or device cannot record in the format MedAuth AI requires. Trying again will not help — use a different browser or device.',
  partial:
    'Part of this encounter reached MedAuth AI before the connection dropped. Trying again resumes recording; it does not recover the audio that was missed.',
} as const;

export interface SessionScreenProps {
  /** Where the patient and provider come from. See `@medauth/fhir-client`. */
  patientSource: PatientSource;
  /**
   * Called once the encounter is closed, with everything the note review screen
   * needs about it (TASK-071).
   *
   * A `CompletedVisit` rather than a bare session because this is the only place
   * all three identifiers are known at once — the session from
   * `POST /sessions/start`, the launch and the chart entry from the resolved
   * subject. See that type for why they stay three named fields.
   */
  onCompleted?: (visit: CompletedVisit) => void;
  sessions?: SessionsApi;
  audioBaseUrl?: string;
  now?: () => number;
}

function describeFailure(failure: ApiFailure, prefix: string): string {
  return `${prefix} ${failure.message}`;
}

/**
 * A refused re-mint, turned into what the provider is told.
 *
 * The 409 is the only status that ends the visit: it means the encounter is
 * already completed, so there is nothing left to refresh a token for. Everything
 * else leaves the encounter open and is reported as a failure to refresh.
 */
function remintFailure(failure: ApiFailure): SessionStatus {
  if (failure.kind === 'status' && failure.status === 409) {
    return { kind: 'failed', message: VISIT_COMPLETED_MESSAGE };
  }
  return {
    kind: 'failed',
    message: describeFailure(failure, 'The session could not be refreshed.'),
  };
}

function Action({
  label,
  onClick,
  testId,
}: {
  label: string;
  onClick: () => void;
  testId: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testId}
      className="self-start rounded-md bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700"
    >
      {label}
    </button>
  );
}

export function SessionScreen({
  patientSource,
  onCompleted,
  sessions = sessionsApi,
  audioBaseUrl = AUDIO_INGESTION_WS_URL,
  now = Date.now,
}: SessionScreenProps) {
  const [sessionStatus, setSessionStatus] = useState<SessionStatus>({ kind: 'none' });

  /** The subject this visit was started for, kept for `onCompleted`. */
  const subjectRef = useRef<VisitSubject | null>(null);

  /**
   * Bumped every time capture should be (re)started for the open session.
   *
   * A counter rather than a boolean because a retry after an error, and the
   * second pass after a proactive refresh, both need to re-enter a start that
   * has already run once for this session.
   */
  const [attempt, setAttempt] = useState(0);

  /**
   * Every nudge raised this visit, accumulated rather than mirrored.
   *
   * The overlay reports its *live* list, which shrinks as a provider dismisses
   * banners. Dismissing means the alert was seen, never that the gap it named
   * was filled, so the checklist keeps entries the overlay has dropped.
   */
  const [flagged, setFlagged] = useState<Nudge[]>([]);

  const held =
    sessionStatus.kind === 'open' || sessionStatus.kind === 'ending' ? sessionStatus.session : null;

  const capture = useAudioCapture({
    sessionId: held?.sessionId ?? '',
    jwt: held?.jwt ?? '',
    baseUrl: audioBaseUrl,
  });

  const phase = visitPhase(sessionStatus, capture.state);

  const startedRef = useRef(0);
  /** At most one proactive refresh per start attempt, so a short-lived token cannot loop. */
  const refreshedRef = useRef(false);
  const captureStartRef = useRef(capture.start);

  // Declared before the effect that reads it, so the ref already holds this
  // render's `start` — which closes over this render's token — by the time that
  // effect runs.
  useEffect(() => {
    captureStartRef.current = capture.start;
  }, [capture.start]);

  useEffect(() => {
    if (sessionStatus.kind !== 'open' || attempt === 0 || startedRef.current === attempt) {
      return;
    }
    startedRef.current = attempt;
    const { session } = sessionStatus;

    void (async () => {
      if (!refreshedRef.current && isNearExpiry(session.jwt, now())) {
        refreshedRef.current = true;
        const refreshed = await sessions.remintToken(session.sessionId, session.jwt);
        if (!refreshed.ok) {
          setSessionStatus(remintFailure(refreshed.failure));
          return;
        }
        // The capture hook closed over the old token in the render this effect
        // belongs to. Bump the attempt instead of starting now, so capture runs
        // against a render that holds the fresh one.
        setSessionStatus({ kind: 'open', session: refreshed.value });
        setAttempt((current) => current + 1);
        return;
      }
      await captureStartRef.current();
    })();
  }, [sessionStatus, attempt, sessions, now]);

  const onNudges = useCallback((live: Nudge[]) => {
    setFlagged((current) => {
      const seen = new Set(current.map((nudge) => nudge.nudgeId));
      const added = live.filter((nudge) => !seen.has(nudge.nudgeId));
      return added.length === 0 ? current : [...current, ...added];
    });
  }, []);

  const onStart = useCallback(async () => {
    refreshedRef.current = false;
    setSessionStatus({ kind: 'creating' });

    const subject = await patientSource();
    if (subject === null) {
      setSessionStatus({ kind: 'failed', message: NO_SUBJECT_MESSAGE });
      return;
    }

    // Held so the completed visit can name the launch and the chart entry
    // alongside the session. This is the only point in the app where all three
    // are in hand; re-deriving any of them later would mean holding it under a
    // second name, which CLAUDE.md's "two names for one visit" rejects.
    subjectRef.current = subject;

    // The chart entry and the launch are passed through when the subject has
    // them: together they are what lets the service fill the encounter's payer
    // columns, and either alone leaves them NULL. A standalone launch has no
    // chart entry and legitimately sends neither.
    const started = await sessions.startVisit({
      patientId: subject.patientId,
      providerId: subject.providerId,
      ...(subject.ehrEncounterId === undefined ? {} : { ehrEncounterId: subject.ehrEncounterId }),
      ...(subject.launchId === undefined ? {} : { launchId: subject.launchId }),
    });
    if (!started.ok) {
      setSessionStatus({
        kind: 'failed',
        message: describeFailure(started.failure, 'The visit could not be started.'),
      });
      return;
    }

    setSessionStatus({ kind: 'open', session: started.value });
    setAttempt((current) => current + 1);
  }, [patientSource, sessions]);

  const onRetry = useCallback(async () => {
    if (capture.state.status !== 'error' || sessionStatus.kind !== 'open') {
      return;
    }
    const { session } = sessionStatus;
    refreshedRef.current = false;

    if (recoveryFor(capture.state.error.code).kind === 'remint') {
      const refreshed = await sessions.remintToken(session.sessionId, session.jwt);
      if (!refreshed.ok) {
        setSessionStatus(remintFailure(refreshed.failure));
        return;
      }
      // Refreshing counts as the one proactive refresh for this attempt; the
      // token is seconds old, and checking it again would only re-mint twice.
      refreshedRef.current = true;
      setSessionStatus({ kind: 'open', session: refreshed.value });
    }

    setAttempt((current) => current + 1);
  }, [capture.state, sessionStatus, sessions]);

  const onEnd = useCallback(async () => {
    const session =
      sessionStatus.kind === 'open' || sessionStatus.kind === 'end-failed'
        ? sessionStatus.session
        : null;
    if (session === null) {
      return;
    }

    // The microphone stops first: the provider has said the visit is over, and
    // audio must not keep being captured while the encounter is being closed.
    // `stop()` also drops whatever the framer still holds.
    capture.stop();
    setSessionStatus({ kind: 'ending', session });

    const ended = await sessions.endVisit(session.sessionId);
    setSessionStatus(
      ended.ok
        ? { kind: 'ended', session }
        : { kind: 'end-failed', session, message: END_FAILED_MESSAGE },
    );
  }, [capture, sessionStatus, sessions]);

  function renderPhase() {
    switch (phase.kind) {
      case 'idle':
        return (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-slate-700">Ready to start a visit.</p>
            <Action label="Start visit" onClick={() => void onStart()} testId="start-visit" />
          </div>
        );

      case 'starting':
        return <p className="text-sm text-slate-700">Starting the visit…</p>;

      case 'connecting':
        return (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-slate-700">Connecting the microphone…</p>
            <Action label="End visit" onClick={() => void onEnd()} testId="end-visit" />
          </div>
        );

      case 'recording':
        return (
          <div className="flex flex-col gap-3">
            <p className="text-sm font-semibold text-red-800" data-testid="recording">
              {RECORDING_LABEL}
            </p>
            <Action label="End visit" onClick={() => void onEnd()} testId="end-visit" />
          </div>
        );

      case 'ending':
        return <p className="text-sm text-slate-700">Ending the visit…</p>;

      case 'ended':
        return (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-slate-700" data-testid="visit-ended">
              Visit completed.
            </p>
            {/*
              An explicit action rather than an automatic navigation. Ending a
              visit and opening the note are two decisions, and a provider who
              ended a visit to start the next one should not be taken to a note
              they did not ask for — the note keeps, and its screen is now
              linkable, so nothing is lost by waiting to be asked.
            */}
            {onCompleted !== undefined && sessionStatus.kind === 'ended' && (
              <Action
                label="Review note"
                onClick={() =>
                  onCompleted({
                    session: sessionStatus.session,
                    launchId: subjectRef.current?.launchId ?? null,
                    ehrEncounterId: subjectRef.current?.ehrEncounterId ?? null,
                  })
                }
                testId="review-note"
              />
            )}
          </div>
        );

      case 'capture-failed': {
        const recovery = recoveryFor(phase.error.code);
        return (
          <div className="flex flex-col gap-3" role="alert">
            <p className="text-sm font-semibold text-red-800" data-testid="capture-failed">
              {phase.error.message}
            </p>
            <p className="text-sm text-slate-700">{RECOVERY_GUIDANCE[recovery.kind]}</p>
            {recovery.kind !== 'unsupported' && (
              <Action label="Try again" onClick={() => void onRetry()} testId="retry-capture" />
            )}
            <Action label="End visit" onClick={() => void onEnd()} testId="end-visit" />
          </div>
        );
      }

      case 'visit-failed':
        return (
          <div className="flex flex-col gap-3" role="alert">
            <p className="text-sm font-semibold text-red-800" data-testid="visit-failed">
              {phase.message}
            </p>
            {sessionStatus.kind === 'end-failed' && (
              <Action label="End visit" onClick={() => void onEnd()} testId="end-visit" />
            )}
          </div>
        );

      default: {
        const unhandled: never = phase;
        void unhandled;
        return null;
      }
    }
  }

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
      <h1 className="text-lg font-semibold text-slate-900">Visit</h1>

      {renderPhase()}

      {/*
        Both live surfaces are mounted for as long as the encounter is open, and
        neither is gated on capture being healthy: the audio socket, the
        transcript socket and the nudge socket are three independent
        connections, and hiding the transcript because the microphone failed
        would hide the evidence of what actually reached the server.
      */}
      {held !== null && (
        <>
          <TranscriptStreamPane session={held} />
          <ProcedureChecklist flagged={flagged} />
          <NudgeOverlay sessionId={held.sessionId} jwt={held.jwt} onNudges={onNudges} />
        </>
      )}
    </main>
  );
}

/**
 * The transcript pane and the socket behind it.
 *
 * Split out so the hook is mounted only while an encounter is open, rather than
 * called unconditionally with an empty session id — a hook cannot be called
 * conditionally, and a socket opened against `''` would be a refused handshake
 * on every render of an idle screen.
 */
function TranscriptStreamPane({ session }: { session: Session }) {
  const stream = useTranscriptStream({ sessionId: session.sessionId, jwt: session.jwt });
  return (
    <TranscriptPane state={stream.state} segments={stream.segments} onRetry={stream.retry} />
  );
}
