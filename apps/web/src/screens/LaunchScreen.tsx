/**
 * Obtaining a SMART launch from the browser (TASK-070).
 *
 * The web counterpart of `apps/mobile`'s `LaunchScreen`, and the shape differs
 * for one structural reason: starting a launch here navigates the page away.
 * There is no awaited result and no cancellation to observe — the provider
 * either comes back with a claim code in the URL, which `App` redeems before
 * this screen is ever rendered again, or does not come back at all.
 *
 * **An EHR-initiated launch never reaches this screen.** The EHR opens
 * `GET /fhir/launch` itself, carrying `iss` and `launch`, and the provider
 * arrives here holding a claim code. What this screen offers is the standalone
 * case: a provider who opened MedAuth directly and has to say which EHR to sign
 * in to.
 *
 * **With no issuer configured there is no standalone launch to offer, and the
 * screen says so.** `SMART_ISS` empty is a real state — a deployment that has
 * not named an EHR — and launching against an empty issuer would fail at SMART
 * discovery in a way that reads as the EHR being down. See CLAUDE.md, "Which EHR
 * a client-initiated standalone launch targets", for why one configured issuer
 * is a scope limit rather than a design.
 */

import { launchApi as defaultLaunchApi, type LaunchApi } from '../api/fhirClient';
import { SMART_ISS } from '../config';
import { beginLaunch, type Navigate } from '../launch/smartLaunch';

export const NO_ISSUER_MESSAGE =
  'MedAuth AI has no EHR configured to sign in to, so a visit cannot be started here. Open MedAuth AI from a patient chart in the EHR instead.';

export interface LaunchScreenProps {
  /** A message from a launch that did not complete, or null. */
  failure?: string | null;
  iss?: string;
  launches?: LaunchApi;
  /** Injected in tests; the default navigates this page. */
  navigate?: Navigate;
}

function navigateHere(url: string): void {
  window.location.assign(url);
}

export function LaunchScreen({
  failure = null,
  iss = SMART_ISS,
  launches = defaultLaunchApi,
  navigate = navigateHere,
}: LaunchScreenProps) {
  const canLaunch = iss !== '';

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6" data-testid="launch-screen">
      <h1 className="text-lg font-semibold text-slate-900">MedAuth AI</h1>

      {failure !== null && (
        <p role="alert" className="text-sm font-semibold text-red-800" data-testid="launch-failed">
          {failure}
        </p>
      )}

      {canLaunch ? (
        <>
          <p className="text-sm text-slate-700">
            Sign in to the EHR to start a visit. You will be taken to the EHR and returned here.
          </p>
          <button
            type="button"
            data-testid="begin-launch"
            onClick={() => beginLaunch(launches, { iss }, navigate)}
            className="self-start rounded-md bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700"
          >
            Sign in to the EHR
          </button>
        </>
      ) : (
        // No button at all, rather than one that cannot work. The same refusal
        // apps/mobile makes, and for the same reason.
        <p className="text-sm text-slate-700" data-testid="no-issuer">
          {NO_ISSUER_MESSAGE}
        </p>
      )}
    </main>
  );
}
