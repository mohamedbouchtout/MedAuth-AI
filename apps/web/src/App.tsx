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
 * **TASK-071 is that trigger firing, so this app now has a router.** The
 * condition named here was "a requirement that the note review screen be
 * linkable or survive a refresh", and the note review screen is that
 * requirement: a provider interrupted during chart review is an ordinary event
 * in a clinic rather than an edge case, and a review screen that exists only as
 * an in-memory phase of one visit is lost to any reload. So `/notes/:sessionId`
 * is a route, and the visit flow is what everything else renders.
 *
 * **The claim code is still read here, above the routes, and that has not
 * changed.** TASK-051f's redirect lands at whatever path `SMART_WEB_RETURN_URL`
 * names, and `launch/inbound.ts` is explicit that this app must not hold a
 * second copy of that setting or discard a completed launch whose path it did
 * not expect. So an arriving launch is still a fact about the URL this page
 * booted at rather than a route, and routing only decides what is rendered once
 * no launch is arriving. `scrubLaunchParams` touches the query string and leaves
 * the path alone, so the two do not interfere.
 *
 * **The note route is deliberately outside the launch gate.** The note routes
 * take no credential in v1, so a reloaded or linked review screen must render
 * without a launch — with the chart write reporting that it holds none, which is
 * a state rather than an error. Putting it behind the gate would show a provider
 * the sign-in screen for a note they can legitimately read.
 *
 * The launch is held in memory for the life of the page and never persisted: a
 * `launch_id` resolves to an EHR access token, so it is a credential by this
 * repository's own definition. That is exactly why a reload loses it.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Route, Routes, useNavigate } from 'react-router';

import { launchApi as defaultLaunchApi, type LaunchApi } from './api/fhirClient';
import { readClaim, readLaunchFailure, scrubLaunchParams } from './launch/inbound';
import { completeLaunch, messageForFailure } from './launch/smartLaunch';
import { LaunchScreen } from './screens/LaunchScreen';
import { NoteReviewRoute } from './screens/NoteReviewRoute';
import { VisitFlow } from './screens/VisitFlow';
import type { CompletedVisit } from './session/completedVisit';

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
  | { kind: 'launched'; launchId: string };

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
  // A failed launch comes back the same way a completed one does, carrying an
  // `error` where a claim code would be (TASK-051g). The two are mutually
  // exclusive by construction — a failure names no launch, so there is nothing
  // to mint a code against — and the claim is read first so a completed launch
  // could never be discarded in favour of an error some other party appended.
  const launchFailure = claim === null ? readLaunchFailure(location.search) : null;

  const navigate = useNavigate();

  /**
   * The visit this page most recently closed, or null.
   *
   * Held here rather than passed through the router's location state so that a
   * `launch_id` never reaches `history.state`, and so the review screen can tell
   * a visit it has context for from one it was linked to. See `CompletedVisit`
   * for why its three identifiers stay three named fields.
   */
  const [completed, setCompleted] = useState<CompletedVisit | null>(null);

  const [state, setState] = useState<AppState>(() => {
    if (claim !== null) {
      return { kind: 'redeeming' };
    }
    // Distinct from a plain load, which is the whole point: before TASK-051g
    // both looked like the sign-in screen with nothing said, and a provider who
    // had just been refused by the EHR was invited to try again with no
    // indication that anything had happened.
    return {
      kind: 'no-launch',
      failure: launchFailure === null ? null : messageForFailure(launchFailure),
    };
  });

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
    scrubLaunchParams(history, location);

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

  /**
   * Take a reported failure out of the URL once it has been rendered.
   *
   * The redemption effect below already scrubs, but it returns early when there
   * is no claim — so without this an `?error=` would sit in the address bar and
   * a reload would replay a failure the provider has already been told about.
   * The message itself survives, because it is held in state rather than
   * re-derived from the URL on every render.
   */
  useEffect(() => {
    if (launchFailure === null) {
      return;
    }
    scrubLaunchParams(history, location);
  }, [launchFailure, history, location]);

  const launchId = state.kind === 'launched' ? state.launchId : null;

  /**
   * Ending a visit navigates to its note.
   *
   * The session goes in the path — it is the only identifier this service
   * exposes to clients and every note route is keyed on it — and the rest of the
   * completed visit is kept in memory. The launch in particular never goes in a
   * URL: it resolves to an EHR access token.
   */
  const onCompleted = useCallback(
    (visit: CompletedVisit) => {
      setCompleted(visit);
      void navigate(`/notes/${visit.session.sessionId}`);
    },
    [navigate],
  );

  const startAnotherVisit = useCallback(() => void navigate('/'), [navigate]);

  /**
   * Everything that is a phase of one visit rather than a destination.
   *
   * The launch gate lives here and not around the whole app: the note route
   * below is reachable without a launch, because the note routes take no
   * credential in v1 and a linked or reloaded review screen is a legitimate way
   * to arrive at one.
   */
  let visit: React.ReactNode;
  if (state.kind === 'redeeming') {
    visit = (
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
        <h1 className="text-lg font-semibold text-slate-900">MedAuth AI</h1>
        <p className="text-sm text-slate-700" data-testid="redeeming">
          Completing the EHR sign-in…
        </p>
      </main>
    );
  } else if (state.kind === 'no-launch') {
    visit = <LaunchScreen failure={state.failure} />;
  } else {
    visit = <VisitFlow launchId={state.launchId} onCompleted={onCompleted} />;
  }

  return (
    <Routes>
      <Route
        path="/notes/:sessionId"
        element={
          <NoteReviewRoute
            completed={completed}
            launchId={launchId}
            onStartAnotherVisit={startAnotherVisit}
          />
        }
      />
      <Route path="*" element={visit} />
    </Routes>
  );
}
