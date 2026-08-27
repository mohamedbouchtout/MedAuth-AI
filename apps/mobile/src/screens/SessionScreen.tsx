/**
 * The session screen — start a visit, record it, end it.
 *
 * This is the mobile half of the "start visit" flow (TASK-025). It calls
 * `POST /sessions/start` (TASK-006), hands the session id and token to
 * `useAudioCapture` (TASK-022), and calls `POST /sessions/{id}/end` when the
 * provider is done.
 *
 * Two things here are requirements rather than presentation choices:
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
 * for a token forks one visit into two encounters with nothing erroring. See
 * CLAUDE.md, "A visit outlasting the token re-mints", which settles this for
 * both session screens; it is not re-derived here.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Linking, Pressable, StyleSheet, Text, View } from 'react-native';

import { isNearExpiry } from '../api/jwt';
import { sessionsApi, type ApiFailure, type SessionsApi } from '../api/sessions';
import { AUDIO_INGESTION_WS_URL } from '../config';
import { useAudioCapture } from '../hooks/useAudioCapture';
import type { PatientSource } from '../session/patientSource';
import { recoveryFor } from '../session/recovery';
import { visitPhase, type SessionStatus } from '../session/visitPhase';

export const NO_SUBJECT_MESSAGE =
  'MedAuth AI cannot start a visit yet: this build has no way to identify the patient and provider. Patient selection arrives in TASK-025b.';

export const VISIT_COMPLETED_MESSAGE =
  'This visit has already been completed and cannot be reopened. Start a new visit.';

export const END_FAILED_MESSAGE =
  'Recording has stopped, but the visit could not be closed. Try ending it again.';

const RECORDING_LABEL = 'Recording in progress';

/** Guidance the error's own message does not carry, keyed by what to do next. */
const RECOVERY_GUIDANCE = {
  retry: 'Nothing was recorded. Try again.',
  permission: 'Grant microphone access in Settings, then try again.',
  remint: 'The session token was refused. Trying again refreshes it first.',
  unsupported:
    'This device cannot record in the format MedAuth AI requires. Trying again will not help — use a different device.',
  partial:
    'Part of this encounter reached MedAuth AI before the connection dropped. Trying again resumes recording; it does not recover the audio that was missed.',
} as const;

export interface SessionScreenProps {
  /** Where the patient and provider come from. See `patientSource` — TASK-025b. */
  patientSource: PatientSource;
  sessions?: SessionsApi;
  audioBaseUrl?: string;
  now?: () => number;
  openSettings?: () => void;
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
  return { kind: 'failed', message: describeFailure(failure, 'The session could not be refreshed.') };
}

export function SessionScreen({
  patientSource,
  sessions = sessionsApi,
  audioBaseUrl = AUDIO_INGESTION_WS_URL,
  now = Date.now,
  openSettings,
}: SessionScreenProps): React.JSX.Element {
  const [sessionStatus, setSessionStatus] = useState<SessionStatus>({ kind: 'none' });
  /**
   * Bumped every time capture should be (re)started for the open session.
   *
   * A counter rather than a boolean because a retry after an error, and the
   * second pass after a proactive refresh, both need to re-enter a start that
   * has already run once for this session.
   */
  const [attempt, setAttempt] = useState(0);

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

  const onStart = useCallback(async () => {
    refreshedRef.current = false;
    setSessionStatus({ kind: 'creating' });

    const subject = await patientSource();
    if (subject === null) {
      setSessionStatus({ kind: 'failed', message: NO_SUBJECT_MESSAGE });
      return;
    }

    const started = await sessions.startVisit({
      patientId: subject.patientId,
      providerId: subject.providerId,
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
      ended.ok ? { kind: 'ended' } : { kind: 'end-failed', session, message: END_FAILED_MESSAGE },
    );
  }, [capture, sessionStatus, sessions]);

  const onOpenSettings = useCallback(() => {
    if (openSettings) {
      openSettings();
      return;
    }
    void Linking.openSettings();
  }, [openSettings]);

  function renderPhase(): React.JSX.Element {
    switch (phase.kind) {
      case 'idle':
        return (
          <View style={styles.block}>
            <Text style={styles.body}>Ready to start a visit.</Text>
            <Action label="Start visit" onPress={onStart} testID="start-visit" />
          </View>
        );

      case 'starting':
        return <Text style={styles.body}>Starting the visit…</Text>;

      case 'connecting':
        return (
          <View style={styles.block}>
            <Text style={styles.body}>Connecting the microphone…</Text>
            <Action label="End visit" onPress={onEnd} testID="end-visit" />
          </View>
        );

      case 'recording':
        return (
          <View style={styles.block}>
            <Text style={styles.recording}>{RECORDING_LABEL}</Text>
            <Action label="End visit" onPress={onEnd} testID="end-visit" />
          </View>
        );

      case 'ending':
        return <Text style={styles.body}>Ending the visit…</Text>;

      case 'ended':
        return (
          <View style={styles.block}>
            <Text style={styles.body}>Visit ended.</Text>
            <Action label="Start visit" onPress={onStart} testID="start-visit" />
          </View>
        );

      case 'capture-failed': {
        const recovery = recoveryFor(phase.error.code);
        return (
          <View style={styles.block} accessibilityRole="alert" testID="capture-error">
            <Text style={styles.errorTitle}>This visit is not recording.</Text>
            <Text style={styles.body}>{phase.error.message}</Text>
            <Text style={styles.body}>{RECOVERY_GUIDANCE[recovery.kind]}</Text>
            {recovery.kind === 'permission' ? (
              <Action label="Open settings" onPress={onOpenSettings} testID="open-settings" />
            ) : null}
            {/*
              No retry on hardware that cannot capture the format: the same
              attempt would fail the same way, and offering it reads as though
              the failure were transient.
            */}
            {recovery.kind === 'unsupported' ? null : (
              <Action label="Try again" onPress={onRetry} testID="retry-capture" />
            )}
            <Action label="End visit" onPress={onEnd} testID="end-visit" />
          </View>
        );
      }

      case 'visit-failed':
        return (
          <View style={styles.block} accessibilityRole="alert" testID="visit-error">
            <Text style={styles.errorTitle}>The visit could not continue.</Text>
            <Text style={styles.body}>{phase.message}</Text>
            {sessionStatus.kind === 'end-failed' ? (
              <Action label="Try ending again" onPress={onEnd} testID="retry-end" />
            ) : (
              <Action label="Start a new visit" onPress={onStart} testID="start-visit" />
            )}
          </View>
        );

      default: {
        const unhandled: never = phase;
        void unhandled;
        return <Text style={styles.body}>Ready to start a visit.</Text>;
      }
    }
  }

  return (
    <View style={styles.container} testID="session-screen">
      <Text style={styles.title}>MedAuth AI</Text>
      {renderPhase()}
    </View>
  );
}

function Action({
  label,
  onPress,
  testID,
}: {
  label: string;
  onPress: () => void | Promise<void>;
  testID: string;
}): React.JSX.Element {
  return (
    <Pressable
      accessibilityRole="button"
      testID={testID}
      style={styles.action}
      onPress={() => {
        void onPress();
      }}
    >
      <Text style={styles.actionLabel}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
  },
  block: {
    alignItems: 'center',
    gap: 12,
  },
  title: {
    fontSize: 24,
    fontWeight: '600',
    marginBottom: 16,
  },
  body: {
    textAlign: 'center',
  },
  recording: {
    fontSize: 18,
    fontWeight: '600',
  },
  errorTitle: {
    fontSize: 18,
    fontWeight: '600',
    textAlign: 'center',
  },
  action: {
    borderRadius: 8,
    borderWidth: 1,
    paddingHorizontal: 20,
    paddingVertical: 12,
  },
  actionLabel: {
    fontSize: 16,
    fontWeight: '500',
  },
});
