/**
 * The `/prior-auth` route, and the one thing it has to establish first (TASK-072).
 *
 * **The queue is scoped to a provider, so this route needs a provider identity
 * before it can ask for anything.** That comes from the launch context, already
 * resolved to a local `provider_id` — this app never handles a practitioner
 * reference, and never asserts a provider identity of its own.
 *
 * Two states therefore come before the dashboard, and both are explained rather
 * than rendered as an empty queue:
 *
 * * **No launch.** A `launch_id` is never persisted — it resolves to an EHR
 *   access token — so a reloaded or linked `/prior-auth` has none. That is an
 *   ordinary state, and the honest thing to say is that signing in through the
 *   EHR is what fills this screen. Showing an empty list instead would tell a
 *   provider they have no outstanding authorizations, which is a different claim
 *   and might be false.
 * * **A launch whose actor was never verified.** TASK-051c records a
 *   practitioner reference only after checking the EHR's `id_token` against its
 *   published keys, so a null `provider_id` means we do not know who is asking.
 *   There is no query to make, and the same message the patient picker uses says
 *   so — one wording for one situation.
 *
 * This is the same rule the note route follows in the other direction: a screen
 * says what is true about the state it is in rather than offering a control that
 * can only fail.
 */

import { useEffect, useState } from 'react';

import { resolveLaunch } from '@medauth/fhir-client';

import { fhirApi as defaultFhirApi } from '../api/fhirClient';
import type { PriorAuthApi } from '../api/priorAuth';
import { NO_PROVIDER_MESSAGE } from './PatientPicker';
import { PriorAuthDashboard } from './PriorAuthDashboard';

/** What is known about who is asking. */
type ProviderState =
  | { kind: 'resolving' }
  | { kind: 'resolved'; providerId: string }
  | { kind: 'unavailable'; message: string };

const NO_LAUNCH_MESSAGE =
  'Sign in from your EHR to see the prior authorizations from your visits. ' +
  'MedAuth AI does not keep an EHR sign-in across page loads.';

/**
 * Where a render starts, given what this page holds.
 *
 * A launch that is absent is answered immediately rather than after a round
 * trip: there is nothing to ask.
 */
function initialState(launchId: string | null): ProviderState {
  return launchId === null
    ? { kind: 'unavailable', message: NO_LAUNCH_MESSAGE }
    : { kind: 'resolving' };
}

export interface PriorAuthRouteProps {
  /** The launch this page holds, or null after a reload or a direct link. */
  launchId: string | null;
  fhir?: typeof defaultFhirApi;
  api?: PriorAuthApi;
  onStartAnotherVisit?: () => void;
}

export function PriorAuthRoute({
  launchId,
  fhir = defaultFhirApi,
  api,
  onStartAnotherVisit,
}: PriorAuthRouteProps) {
  const [state, setState] = useState<ProviderState>(() => initialState(launchId));

  /**
   * Which launch the held state describes.
   *
   * Reset during render rather than in the effect below — React's own
   * recommendation for adjusting state when a prop changes, and the same shape
   * `usePriorAuthQueue` uses. It also keeps the effect free of a synchronous
   * `setState`, which would be a cascading render on every pass.
   */
  const [resolvedFor, setResolvedFor] = useState(launchId);
  if (launchId !== resolvedFor) {
    setResolvedFor(launchId);
    setState(initialState(launchId));
  }

  useEffect(() => {
    if (launchId === null) {
      return;
    }

    let cancelled = false;

    void (async () => {
      const resolution = await resolveLaunch(fhir, launchId);
      if (cancelled) {
        return;
      }
      if (resolution.kind === 'failed') {
        setState({ kind: 'unavailable', message: resolution.failure.message });
        return;
      }
      if (resolution.kind === 'no-provider') {
        setState({ kind: 'unavailable', message: NO_PROVIDER_MESSAGE });
        return;
      }
      // Both remaining resolutions carry a provider: the EHR named a patient,
      // or it did not and the visit would have gone to a search. Which of the
      // two is about *this* visit's patient and says nothing about the queue.
      setState({
        kind: 'resolved',
        providerId:
          resolution.kind === 'resolved'
            ? resolution.subject.providerId
            : resolution.providerId,
      });
    })();

    return () => {
      cancelled = true;
    };
  }, [fhir, launchId]);

  if (state.kind === 'resolved') {
    return (
      <PriorAuthDashboard
        providerId={state.providerId}
        {...(api === undefined ? {} : { api })}
        {...(onStartAnotherVisit === undefined ? {} : { onStartAnotherVisit })}
      />
    );
  }

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
      <h1 className="text-lg font-semibold text-slate-900">Prior authorizations</h1>
      {state.kind === 'resolving' ? (
        <p className="text-sm text-slate-700" data-testid="queue-resolving">
          Checking your EHR sign-in…
        </p>
      ) : (
        <p className="text-sm text-slate-700" data-testid="queue-unavailable">
          {state.message}
        </p>
      )}
      {onStartAnotherVisit !== undefined && (
        <button
          type="button"
          onClick={onStartAnotherVisit}
          className="self-start text-sm font-medium text-blue-700 underline"
        >
          Back to visits
        </button>
      )}
    </main>
  );
}
