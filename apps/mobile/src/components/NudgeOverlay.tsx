/**
 * `<NudgeOverlay />` — the live payer-rule alerts a provider sees mid-encounter,
 * on a device that can buzz.
 *
 * It is `apps/web`'s overlay with one addition and one substitution: a nudge
 * marked for escalation triggers a haptic, and React Native primitives stand in
 * for DOM ones. The payload it renders, the acknowledge call it makes and the
 * socket it listens on are the same, which is why the first two live in
 * `@medauth/nudge-client` rather than in either app.
 *
 * It takes the session id **and the session JWT**: the nudge socket validates
 * the token before completing its handshake, so a component holding no token
 * opens nothing. The prop list follows `useAudioCapture`'s and the web overlay's,
 * so every real-time surface in this repository is wired the same way.
 *
 * What is rendered comes from CLAUDE.md's "The nudge payload — one shape" and
 * nothing else. Three of its properties drive real branches here:
 *
 * - **`haptic` decides the buzz, and `denial_risk` never does.** The decision
 *   lives in `nudges/escalation.ts`, which explains why the two are not
 *   interchangeable and why an outage must not be able to escalate.
 * - **`cpt_code` is nullable.** TASK-044 raises nudges on a keyword that
 *   resolved no CPT code, so the banner names a code only when there is one
 *   rather than rendering an empty slot.
 * - **An empty `missing_criteria` does not mean "nothing is missing".** On the
 *   safe fallback answer it means the criteria are *unknown*, so the banner says
 *   that instead of implying the documentation is complete. Reading silence as
 *   "no action needed" is the one direction the nudge path must not fail in.
 *
 * **Dismissing carries no credential**, per CLAUDE.md's "A route keyed on a
 * resource rather than a session follows the same v1 rule" — see the package's
 * acknowledge client for why sending one anyway would be worse than sending
 * none. A dismissal that fails leaves the banner up: a provider who watched the
 * alert vanish would believe it was recorded as seen, and the audit row that
 * stands in for a credential on that route would not exist.
 *
 * Accessibility: each banner is an `alert` role, which announces on arrival
 * without stealing focus from a provider mid-consultation. Risk is spelled out
 * in text as well as colour, so colour is never the only carrier.
 */

import type { DenialRisk, Nudge } from '@medauth/nudge-client';
import * as Haptics from 'expo-haptics';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { nudgesApi, type NudgesApi } from '../api/nudges';
import { useNudgeStream } from '../hooks/useNudgeStream';
import { shouldBuzz } from '../nudges/escalation';

/** The subset of `expo-haptics` this component uses, so tests can supply their own. */
export interface HapticsLike {
  notificationAsync: (type?: Haptics.NotificationFeedbackType) => Promise<void>;
}

export interface NudgeOverlayProps {
  /** The encounter's session id, from `POST /sessions/start` (TASK-006). */
  sessionId: string;
  /** The session JWT from the same response. Carried as a header, never logged. */
  jwt: string;
  /** WebSocket origin for nudge-service. Defaults to the configured one. */
  baseUrl?: string;
  /** Injected in tests; the default calls track-b-rag. */
  nudges?: NudgesApi;
  /** Injected in tests; the default calls track-a-clinical's re-mint route. */
  sessions?: NonNullable<Parameters<typeof useNudgeStream>[0]['sessions']>;
  /** Injected in tests, so "near expiry" does not depend on the wall clock. */
  now?: () => number;
  /** Injected in tests. The default is the real device. */
  haptics?: HapticsLike;
}

export const DISMISS_FAILED_MESSAGE =
  'This alert could not be dismissed and is still showing. Try again.';

export const UNKNOWN_CRITERIA_MESSAGE =
  'The payer criteria for this procedure could not be retrieved. Confirm the requirements manually.';

const RISK_LABELS: Record<DenialRisk, string> = {
  low: 'Low denial risk',
  medium: 'Medium denial risk',
  high: 'High denial risk',
};

/**
 * Banner colours, keyed on `denial_risk`.
 *
 * Deliberately high contrast: this is read at arm's length, mid-consultation, on
 * a phone held in one hand. Each level pairs a saturated border with dark text
 * on a pale ground rather than relying on hue alone — the risk level is spelled
 * out in the banner's own text as well.
 */
const RISK_STYLES: Record<DenialRisk, { borderColor: string; backgroundColor: string }> = {
  low: { borderColor: '#a16207', backgroundColor: '#fefce8' },
  medium: { borderColor: '#c2410c', backgroundColor: '#fff7ed' },
  high: { borderColor: '#b91c1c', backgroundColor: '#fef2f2' },
};

const RISK_TEXT: Record<DenialRisk, string> = {
  low: '#422006',
  medium: '#431407',
  high: '#450a0a',
};

export function NudgeOverlay({
  sessionId,
  jwt,
  baseUrl,
  nudges: nudgesClient = nudgesApi,
  sessions,
  now,
  haptics = Haptics,
}: NudgeOverlayProps): React.JSX.Element | null {
  const stream = useNudgeStream({
    sessionId,
    jwt,
    ...(baseUrl === undefined ? {} : { baseUrl }),
    ...(sessions === undefined ? {} : { sessions }),
    ...(now === undefined ? {} : { now }),
  });

  const [dismissing, setDismissing] = useState<string | null>(null);
  const [dismissFailed, setDismissFailed] = useState<string | null>(null);

  /**
   * The nudges already escalated on this stream.
   *
   * A ref rather than state: buzzing is a side effect, not something rendered,
   * and re-rendering because a device vibrated would be noise. Keyed by
   * `nudge_id` so a re-render — or a republish carrying the same id — cannot
   * buzz twice for one alert.
   */
  const buzzedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    for (const nudge of stream.nudges) {
      if (buzzedRef.current.has(nudge.nudgeId) || !shouldBuzz(nudge)) {
        continue;
      }
      buzzedRef.current.add(nudge.nudgeId);
      // Fire and forget, and swallow a rejection: a device with no haptic
      // engine, or one refusing while a call is in progress, must not turn a
      // rendered alert into an error. The banner is the alert; the buzz only
      // draws the eye to it.
      void haptics
        .notificationAsync(Haptics.NotificationFeedbackType.Warning)
        .catch(() => undefined);
    }
  }, [stream.nudges, haptics]);

  const onDismiss = useCallback(
    async (nudgeId: string) => {
      setDismissing(nudgeId);
      setDismissFailed(null);
      const result = await nudgesClient.acknowledge(nudgeId);
      setDismissing(null);
      if (!result.ok) {
        // The banner stays. See the header note: a dismissal a provider watched
        // succeed, that did not, is worse than one they have to repeat.
        setDismissFailed(nudgeId);
        return;
      }
      buzzedRef.current.delete(nudgeId);
      stream.remove(nudgeId);
    },
    [nudgesClient, stream],
  );

  const hasNudges = stream.nudges.length > 0;
  const streamError = stream.state.status === 'error' ? stream.state.error : null;

  if (!hasNudges && streamError === null) {
    return null;
  }

  return (
    <View style={styles.region} testID="nudge-overlay">
      {streamError === null ? null : (
        <View style={styles.status} accessibilityRole="alert" testID="nudge-stream-error">
          <Text style={styles.statusText}>{streamError.message}</Text>
          {/*
            A completed visit is the one failure with nothing to retry: the
            encounter is over, and offering a reconnect would suggest otherwise.
          */}
          {streamError.code === 'VISIT_COMPLETED' ? null : (
            <Pressable
              accessibilityRole="button"
              onPress={stream.retry}
              style={styles.action}
              testID="nudge-reconnect"
            >
              <Text style={styles.actionLabel}>Reconnect alerts</Text>
            </Pressable>
          )}
        </View>
      )}

      <ScrollView>
        {stream.nudges.map((nudge) => (
          <NudgeBanner
            key={nudge.nudgeId}
            nudge={nudge}
            dismissing={dismissing === nudge.nudgeId}
            failed={dismissFailed === nudge.nudgeId}
            onDismiss={onDismiss}
          />
        ))}
      </ScrollView>
    </View>
  );
}

interface NudgeBannerProps {
  nudge: Nudge;
  dismissing: boolean;
  failed: boolean;
  onDismiss: (nudgeId: string) => void;
}

function NudgeBanner({ nudge, dismissing, failed, onDismiss }: NudgeBannerProps): React.JSX.Element {
  const risk = RISK_STYLES[nudge.denialRisk];
  const textColor = { color: RISK_TEXT[nudge.denialRisk] };

  return (
    <View
      style={[styles.banner, risk]}
      accessibilityRole="alert"
      testID={`nudge-${nudge.nudgeId}`}
    >
      <Text style={[styles.riskLabel, textColor]}>{RISK_LABELS[nudge.denialRisk]}</Text>

      <Text style={[styles.procedure, textColor]}>
        {/*
          The code is named only when there is one. TASK-044 nudges on a keyword
          that resolved no CPT code, and an empty parenthesis would read as a
          rendering fault rather than as an absent code.
        */}
        {nudge.cptCode === null ? nudge.procedure : `${nudge.procedure} (CPT ${nudge.cptCode})`}
      </Text>

      <Text style={[styles.message, textColor]}>{nudge.message}</Text>

      {nudge.missingCriteria.length > 0 ? (
        <View testID={`nudge-criteria-${nudge.nudgeId}`}>
          <Text style={[styles.criteriaHeading, textColor]}>Still undocumented:</Text>
          {nudge.missingCriteria.map((criterion) => (
            <Text key={criterion} style={[styles.criterion, textColor]}>
              {`• ${criterion}`}
            </Text>
          ))}
        </View>
      ) : (
        // Empty is not "nothing is missing" — on the fallback answer it means the
        // criteria are unknown, and saying nothing here would imply the
        // documentation is complete.
        <Text style={[styles.criterion, textColor]} testID={`nudge-unknown-${nudge.nudgeId}`}>
          {UNKNOWN_CRITERIA_MESSAGE}
        </Text>
      )}

      {failed ? (
        <Text style={[styles.dismissError, textColor]} testID={`nudge-dismiss-failed-${nudge.nudgeId}`}>
          {DISMISS_FAILED_MESSAGE}
        </Text>
      ) : null}

      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Dismiss alert for ${nudge.procedure}`}
        disabled={dismissing}
        onPress={() => onDismiss(nudge.nudgeId)}
        style={styles.action}
        testID={`nudge-dismiss-${nudge.nudgeId}`}
      >
        <Text style={styles.actionLabel}>{dismissing ? 'Dismissing…' : 'Dismiss'}</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  region: { gap: 12 },
  status: {
    borderColor: '#334155',
    borderWidth: 2,
    backgroundColor: '#f1f5f9',
    borderRadius: 8,
    padding: 12,
    gap: 8,
  },
  statusText: { color: '#0f172a', fontSize: 15 },
  banner: { borderWidth: 3, borderRadius: 8, padding: 14, gap: 6, marginBottom: 12 },
  riskLabel: { fontSize: 13, fontWeight: '700', textTransform: 'uppercase' },
  procedure: { fontSize: 19, fontWeight: '700' },
  message: { fontSize: 16 },
  criteriaHeading: { fontSize: 15, fontWeight: '600', marginTop: 4 },
  criterion: { fontSize: 15 },
  dismissError: { fontSize: 15, fontWeight: '600', marginTop: 4 },
  action: {
    marginTop: 8,
    alignSelf: 'flex-start',
    backgroundColor: '#0f172a',
    borderRadius: 6,
    paddingHorizontal: 16,
    paddingVertical: 10,
  },
  actionLabel: { color: '#f8fafc', fontSize: 16, fontWeight: '600' },
});
