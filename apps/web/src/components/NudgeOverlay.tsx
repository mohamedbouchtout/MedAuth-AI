/**
 * `<NudgeOverlay />` — the live payer-rule alerts a provider sees mid-encounter.
 *
 * It takes the session id **and the session JWT**: the nudge socket validates
 * the token before completing its handshake, so a component holding no token
 * opens nothing. The prop list follows `useAudioCapture`'s, this app's other
 * real-time surface, so the two are wired the same way.
 *
 * What is rendered comes from CLAUDE.md's "The nudge payload — one shape" and
 * nothing else. Two of its properties drive real branches here:
 *
 * - **`cpt_code` is nullable.** TASK-044 raises nudges on a keyword that
 *   resolved no CPT code, so the banner names a code only when there is one
 *   rather than rendering an empty slot.
 * - **An empty `missing_criteria` does not mean "nothing is missing".** On the
 *   safe fallback answer it means the criteria are *unknown*, so the banner says
 *   that instead of implying the documentation is complete. Reading silence as
 *   "no action needed" is the one direction the nudge path must not fail in.
 *
 * **Dismissing carries no credential**, per CLAUDE.md's "A route keyed on a
 * resource rather than a session follows the same v1 rule" — see `api/nudges.ts`
 * for why sending one anyway would be worse than sending none.
 *
 * Accessibility: each banner is a `role="alert"`, which announces on arrival
 * without stealing focus from a provider who is typing — the correct behaviour
 * for something that appears unbidden during a consultation. Focus management is
 * about *removal* instead: dismissing a banner moves focus to the next one's
 * dismiss button, or to the region itself, so keyboard focus is never dropped
 * onto `document.body` by a node disappearing.
 */

import { useCallback, useRef, useState } from 'react';

import { nudgesApi, type NudgesApi } from '../api/nudges';
import { useNudgeStream } from '../hooks/useNudgeStream';
import type { DenialRisk, Nudge } from '@medauth/nudge-client';

export interface NudgeOverlayProps {
  /** The encounter's session id, from `POST /sessions/start` (TASK-006). */
  sessionId: string;
  /** The session JWT from the same response. Carried as a subprotocol, never logged. */
  jwt: string;
  /** WebSocket origin for nudge-service. Defaults to the configured one. */
  baseUrl?: string;
  /** Injected in tests; the default calls track-b-rag. */
  nudges?: NudgesApi;
  /** Injected in tests; the default calls track-a-clinical's re-mint route. */
  sessions?: NonNullable<Parameters<typeof useNudgeStream>[0]['sessions']>;
  /** Injected in tests, so "near expiry" does not depend on the wall clock. */
  now?: () => number;
}

/**
 * Banner colours, keyed on `denial_risk`.
 *
 * Deliberately high contrast: this is read at arm's length, mid-consultation, on
 * whatever display the exam room has. Each level pairs a saturated border and a
 * dark text colour against a pale ground rather than relying on hue alone —
 * colour is not the only carrier, since the risk level is also spelled out in
 * the banner's own text.
 */
const RISK_STYLES: Record<DenialRisk, string> = {
  low: 'border-yellow-600 bg-yellow-50 text-yellow-950',
  medium: 'border-orange-600 bg-orange-50 text-orange-950',
  high: 'border-red-700 bg-red-50 text-red-950',
};

const RISK_LABELS: Record<DenialRisk, string> = {
  low: 'Low denial risk',
  medium: 'Medium denial risk',
  high: 'High denial risk',
};

interface NudgeBannerProps {
  nudge: Nudge;
  dismissing: boolean;
  failure: string | null;
  onDismiss: (nudge: Nudge) => void;
  buttonRef: (element: HTMLButtonElement | null) => void;
}

function NudgeBanner({ nudge, dismissing, failure, onDismiss, buttonRef }: NudgeBannerProps) {
  return (
    <li
      role="alert"
      className={`rounded-md border-l-4 p-4 shadow-md ${RISK_STYLES[nudge.denialRisk]}`}
    >
      <p className="text-xs font-semibold tracking-wide uppercase">
        {RISK_LABELS[nudge.denialRisk]}
      </p>

      <p className="mt-1 text-base font-semibold">
        {nudge.procedure}
        {/* Only when the procedure resolved one — TASK-044 nudges without a code. */}
        {nudge.cptCode !== null && (
          <span className="ml-2 font-mono text-sm font-normal">CPT {nudge.cptCode}</span>
        )}
      </p>

      <p className="mt-2 text-sm">{nudge.message}</p>

      {nudge.missingCriteria.length > 0 ? (
        <>
          <p className="mt-3 text-sm font-semibold">Still undocumented:</p>
          <ul className="mt-1 list-disc pl-5 text-sm">
            {nudge.missingCriteria.map((criterion) => (
              <li key={criterion}>{criterion}</li>
            ))}
          </ul>
        </>
      ) : (
        // Empty is not "nothing is missing" — on a fallback answer the criteria
        // were never retrieved. Saying so is the difference between a provider
        // checking and a provider assuming.
        <p className="mt-3 text-sm italic">
          No criteria list was available for this plan — confirm the requirements before ordering.
        </p>
      )}

      {failure !== null && <p className="mt-3 text-sm font-semibold">{failure}</p>}

      <button
        ref={buttonRef}
        type="button"
        onClick={() => onDismiss(nudge)}
        disabled={dismissing}
        className="mt-3 rounded border border-current px-3 py-1 text-sm font-semibold disabled:opacity-60"
      >
        {dismissing ? 'Dismissing…' : 'Dismiss'}
      </button>
    </li>
  );
}

export function NudgeOverlay({
  sessionId,
  jwt,
  baseUrl,
  nudges: nudgesApiOverride = nudgesApi,
  sessions,
  now,
}: NudgeOverlayProps) {
  const stream = useNudgeStream({
    sessionId,
    jwt,
    ...(baseUrl === undefined ? {} : { baseUrl }),
    ...(sessions === undefined ? {} : { sessions }),
    ...(now === undefined ? {} : { now }),
  });

  const [dismissing, setDismissing] = useState<string | null>(null);
  const [failures, setFailures] = useState<Record<string, string>>({});

  const regionRef = useRef<HTMLElement | null>(null);
  const buttonsRef = useRef(new Map<string, HTMLButtonElement>());

  const registerButton = useCallback(
    (nudgeId: string) => (element: HTMLButtonElement | null) => {
      if (element === null) {
        buttonsRef.current.delete(nudgeId);
      } else {
        buttonsRef.current.set(nudgeId, element);
      }
    },
    [],
  );

  const { nudges, remove } = stream;

  const onDismiss = useCallback(
    async (nudge: Nudge) => {
      setDismissing(nudge.nudgeId);
      const result = await nudgesApiOverride.acknowledge(nudge.nudgeId);
      setDismissing(null);

      if (!result.ok) {
        // The banner stays. A provider who clicked dismiss and saw the alert
        // vanish would believe it was recorded as seen, and the audit row that
        // stands in for a credential on that route would not exist.
        setFailures((current) => ({
          ...current,
          [nudge.nudgeId]: `This alert could not be dismissed. ${result.failure.message}`,
        }));
        return;
      }

      // The next banner's button, chosen before the node goes away; falling back
      // to the region so focus is never dropped onto document.body.
      const remaining = nudges.filter((candidate) => candidate.nudgeId !== nudge.nudgeId);
      const next = remaining[0];
      const target = next ? buttonsRef.current.get(next.nudgeId) : undefined;

      remove(nudge.nudgeId);
      setFailures((current) => {
        const rest = { ...current };
        delete rest[nudge.nudgeId];
        return rest;
      });
      (target ?? regionRef.current)?.focus();
    },
    [nudges, nudgesApiOverride, remove],
  );

  const { state } = stream;
  const disconnected = state.status === 'error';

  if (nudges.length === 0 && !disconnected) {
    return null;
  }

  return (
    <section
      ref={regionRef}
      tabIndex={-1}
      aria-label="Prior authorization alerts"
      className="fixed top-4 right-4 z-50 flex w-full max-w-md flex-col gap-3 outline-none"
    >
      {disconnected && (
        // Not a nudge, and deliberately not styled as one: a provider must not
        // read a quiet stream as "nothing to flag" when it is in fact down.
        <p role="status" className="rounded-md border-l-4 border-slate-700 bg-slate-100 p-4 text-sm">
          {state.error.message}{' '}
          {state.error.code !== 'VISIT_COMPLETED' && (
            <button
              type="button"
              onClick={stream.retry}
              className="font-semibold underline underline-offset-2"
            >
              Reconnect
            </button>
          )}
        </p>
      )}

      <ul className="flex flex-col gap-3">
        {nudges.map((nudge) => (
          <NudgeBanner
            key={nudge.nudgeId}
            nudge={nudge}
            dismissing={dismissing === nudge.nudgeId}
            failure={failures[nudge.nudgeId] ?? null}
            onDismiss={(target) => void onDismiss(target)}
            buttonRef={registerButton(nudge.nudgeId)}
          />
        ))}
      </ul>
    </section>
  );
}
