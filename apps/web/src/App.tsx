/**
 * The provider dashboard's root (TASK-070).
 *
 * **This is the "routing" of this app, and choosing not to install a router was
 * a decision rather than an omission.** The only thing arriving from outside is
 * a completed SMART launch, and it identifies itself by a `claim` query
 * parameter rather than by a path — see `launch/inbound.ts` for why keying on
 * the path would mean holding a second copy of the service's own
 * `SMART_WEB_RETURN_URL` and discarding a launch whenever the two disagreed.
 * Everything after that is a phase of one visit, not a destination: the picker,
 * the session and the completed visit are steps a provider walks through in
 * order, and `apps/mobile` models exactly the same flow with the same union and
 * no router at all.
 *
 * What would change that: a screen a provider needs to *arrive* at rather than
 * reach — TASK-072's prior-auth dashboard is the likely first, since it is a
 * standing view rather than a step in a visit — or a requirement that the note
 * review screen be linkable or survive a refresh. At that point this app takes a
 * router and these phases become routes. Adding one now would be scaffolding for
 * screens whose URLs nobody has yet needed.
 *
 * The launch is held in memory for the life of the page and never persisted: a
 * `launch_id` resolves to an EHR access token, so it is a credential by this
 * repository's own definition.
 */

import { useEffect, useRef, useState } from 'react';

import { launchApi as defaultLaunchApi, type LaunchApi } from './api/fhirClient';
import type { Session } from './api/sessions';
import { readClaim, scrubClaim } from './launch/inbound';
import { completeLaunch } from './launch/smartLaunch';
import { LaunchScreen } from './screens/LaunchScreen';
import { VisitFlow } from './screens/VisitFlow';

/**
 * Where the app is, which is a property of how it was opened.
 *
 * `redeeming` exists so the moment between finding a claim code and holding a
 * launch is not rendered as "no launch" — that would flash the sign-in screen at
 * a provider who has just signed in, and invite them to do it again.
 */
type AppState =
  | { kind: 'no-launch'; failure: string | null }
  | { kind: 'redeeming' }
  | { kind: 'launched'; launchId: string }
  | { kind: 'completed'; session: Session };

export interface AppProps {
  launches?: LaunchApi;
  /** The URL this page was opened at. Injected in tests. */
  location?: Location;
  history?: History;
}

export function App({
  launches = defaultLaunchApi,
  location = window.location,
  history = window.history,
}: AppProps = {}) {
  const claim = readClaim(location.search);

  const [state, setState] = useState<AppState>(
    claim === null ? { kind: 'no-launch', failure: null } : { kind: 'redeeming' },
  );

  /**
   * One redemption per code, ever.
   *
   * A claim code is single-use: a second attempt is a 404 by design, which would
   * present as a failed launch that had in fact succeeded. React's development
   * StrictMode double-invokes effects specifically to surface this class of bug,
   * so the guard is a ref rather than a dependency-array subtlety.
   */
  const redeemedRef = useRef(false);

  useEffect(() => {
    if (claim === null || redeemedRef.current) {
      return;
    }
    redeemedRef.current = true;

    // Out of the address bar and out of session history before the request is
    // even made. The code is a credential, and whether it is redeemed
    // successfully has no bearing on whether it should still be sitting in a URL.
    scrubClaim(history, location);

    let cancelled = false;
    void (async () => {
      const outcome = await completeLaunch(launches, claim);
      if (cancelled) {
        return;
      }
      setState(
        outcome.kind === 'launched'
          ? { kind: 'launched', launchId: outcome.session.launchId }
          : { kind: 'no-launch', failure: outcome.message },
      );
    })();

    return () => {
      cancelled = true;
    };
  }, [claim, history, location, launches]);

  if (state.kind === 'redeeming') {
    return (
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
        <h1 className="text-lg font-semibold text-slate-900">MedAuth AI</h1>
        <p className="text-sm text-slate-700" data-testid="redeeming">
          Completing the EHR sign-in…
        </p>
      </main>
    );
  }

  if (state.kind === 'no-launch') {
    return <LaunchScreen failure={state.failure} />;
  }

  if (state.kind === 'completed') {
    return (
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
        <h1 className="text-lg font-semibold text-slate-900">Visit completed</h1>
        {/*
          TASK-071 builds the note review screen against `GET /notes/{session_id}`.
          Until it exists this names the visit that was closed rather than
          pretending to be that screen — the session id is what TASK-071 is keyed
          on, and it is the one identifier this app hands on.
        */}
        <p className="text-sm text-slate-700" data-testid="awaiting-note-review">
          The note for this visit is being generated. Review arrives in TASK-071.
        </p>
      </main>
    );
  }

  return (
    <VisitFlow
      launchId={state.launchId}
      onCompleted={(session) => setState({ kind: 'completed', session })}
    />
  );
}
