/**
 * Signing in to the EHR, before a patient can be identified (TASK-025c).
 *
 * This is the first screen of the app in every build that has an EHR
 * configured. Everything after it — the patient picker, the session screen — is
 * keyed on the `launch_id` this screen obtains, because every route that names a
 * patient spends the launch's EHR access token.
 *
 * **The two launch types reach this screen differently, and that is the whole
 * of the difference here.** An EHR launch arrives as a deep link already
 * carrying the issuer and the EHR's opaque launch context, and it starts on its
 * own: the provider expressed their intent by opening MedAuth from the chart,
 * and asking them to tap again is asking twice. A standalone launch has only
 * the configured issuer and waits for a tap, because nobody has asked for
 * anything yet.
 *
 * **A cancelled sign-in is not an error and is not rendered as one.** A
 * provider who closes the login window has decided not to start a visit. What
 * they get is a plain statement that nothing was started and the same button
 * they began with — never a red failure implying something broke.
 *
 * **No partially-configured state exists on this screen.** Either it holds a
 * launch and hands it on, or it holds nothing and says so. That is what keeps a
 * patient route from being called with a handle that names no EHR credential,
 * which would surface as a 404 from an unrelated route rather than as a launch
 * that did not happen.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';

import type { LaunchApi, LaunchRequest, LaunchSession } from '../api/launch';
import { launchApi as defaultLaunchApi } from '../api/launchClient';
import { SMART_ISS, SMART_RETURN_URI } from '../config';
import { openAuthSession as defaultOpen } from '../launch/browser';
import { performSmartLaunch, type AuthSessionOpener } from '../launch/smartLaunch';

export const CONNECT_LABEL = 'Sign in to the EHR';

export const LAUNCHING_LABEL = 'Waiting for the EHR sign-in to finish…';

/**
 * What a build with no configured issuer says.
 *
 * Administrator-facing on purpose. A provider cannot act on this, and a message
 * implying they could — "try again", "check your connection" — would send them
 * round a loop that cannot terminate. The one configured issuer this names is a
 * scope limit of the current pilot; see CLAUDE.md, "Which EHR a client-initiated
 * standalone launch targets".
 */
export const NO_ISS_MESSAGE =
  'This build has no EHR configured, so MedAuth AI cannot sign in to one. Report this to your administrator: EXPO_PUBLIC_SMART_ISS is not set.';

export const CANCELLED_MESSAGE = 'EHR sign-in was cancelled, so no visit has been started.';

interface Idle {
  kind: 'idle';
  /** A completed-but-not-launched outcome to state, or null on first render. */
  message: string | null;
  /** Whether `message` reports a failure rather than a cancellation. */
  failed: boolean;
}

interface Launching {
  kind: 'launching';
}

type ScreenState = Idle | Launching;

export interface LaunchScreenProps {
  /** Called once, with the launch this screen obtained. */
  onLaunched: (session: LaunchSession) => void;
  /**
   * The EHR-initiated launch this app was opened with, when it was.
   *
   * Its presence is what distinguishes the two launch types, and it carries the
   * EHR's opaque `launch` context — which is the only reason the completed
   * launch knows which patient is on the chart.
   */
  inbound?: LaunchRequest | null;
  /** The configured issuer, used when no EHR-initiated launch arrived. */
  iss?: string;
  returnUri?: string;
  api?: LaunchApi;
  open?: AuthSessionOpener;
}

export function LaunchScreen({
  onLaunched,
  inbound = null,
  iss = SMART_ISS,
  returnUri = SMART_RETURN_URI,
  api = defaultLaunchApi,
  open = defaultOpen,
}: LaunchScreenProps): React.JSX.Element {
  const [state, setState] = useState<ScreenState>({ kind: 'idle', message: null, failed: false });

  /**
   * Held in a ref for the reason TASK-025b's picker holds its own callback in
   * one: a caller may recreate `onLaunched` on every render, and with it in the
   * dependency array below an EHR launch would be re-attempted on each one —
   * opening a browser window per render.
   */
  const onLaunchedRef = useRef(onLaunched);
  useEffect(() => {
    onLaunchedRef.current = onLaunched;
  }, [onLaunched]);

  /** The request currently in flight, so nothing is attempted twice at once. */
  const inFlight = useRef(false);

  const attempt = useCallback(
    async (request: LaunchRequest) => {
      if (inFlight.current) {
        return;
      }
      inFlight.current = true;
      setState({ kind: 'launching' });
      const outcome = await performSmartLaunch({ request, returnUri, api, open });
      inFlight.current = false;

      switch (outcome.kind) {
        case 'launched':
          // Handed straight on and never held here: this screen keeps no copy
          // of a credential it has already delivered.
          onLaunchedRef.current(outcome.session);
          return;
        case 'cancelled':
          setState({ kind: 'idle', message: CANCELLED_MESSAGE, failed: false });
          return;
        default:
          setState({ kind: 'idle', message: outcome.message, failed: true });
      }
    },
    [api, open, returnUri],
  );

  /**
   * An EHR launch starts itself, once.
   *
   * Guarded on the request rather than on render count: a second deep link
   * genuinely is a second launch — a provider switching charts — and must be
   * honoured, while the same one arriving again through a re-render must not.
   */
  const attempted = useRef<LaunchRequest | null>(null);
  useEffect(() => {
    if (inbound === null || attempted.current === inbound) {
      return;
    }
    attempted.current = inbound;
    void attempt(inbound);
  }, [attempt, inbound]);

  const onPress = useCallback(() => {
    // The standalone path, and the only one that uses the configured issuer.
    void attempt({ iss });
  }, [attempt, iss]);

  if (state.kind === 'launching') {
    return (
      <View style={styles.container} testID="launch-screen">
        <Text style={styles.title}>MedAuth AI</Text>
        <ActivityIndicator accessibilityLabel="Signing in to the EHR" />
        <Text style={styles.body}>{LAUNCHING_LABEL}</Text>
      </View>
    );
  }

  const configured = iss !== '' || inbound !== null;

  return (
    <View style={styles.container} testID="launch-screen">
      <Text style={styles.title}>MedAuth AI</Text>

      {configured ? (
        <Text style={styles.body}>
          MedAuth AI signs in to your EHR to identify the patient and the
          provider. Sign-in opens in your browser.
        </Text>
      ) : (
        <View style={styles.block} accessibilityRole="alert" testID="launch-unconfigured">
          <Text style={styles.errorTitle}>A visit cannot be started.</Text>
          <Text style={styles.body}>{NO_ISS_MESSAGE}</Text>
        </View>
      )}

      {state.message === null ? null : (
        <Text
          style={styles.body}
          accessibilityRole={state.failed ? 'alert' : 'text'}
          testID={state.failed ? 'launch-error' : 'launch-cancelled'}
        >
          {state.message}
        </Text>
      )}

      {configured ? (
        <Pressable
          accessibilityRole="button"
          style={styles.action}
          onPress={onPress}
          testID="start-launch"
        >
          <Text style={styles.actionLabel}>{CONNECT_LABEL}</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, gap: 12, justifyContent: 'center', padding: 24 },
  block: { gap: 8 },
  title: { fontSize: 24, fontWeight: '600' },
  body: { fontSize: 16 },
  errorTitle: { fontSize: 18, fontWeight: '600' },
  action: { alignItems: 'center', backgroundColor: '#1f6feb', borderRadius: 6, padding: 14 },
  actionLabel: { color: '#fff', fontSize: 16, fontWeight: '600' },
});
