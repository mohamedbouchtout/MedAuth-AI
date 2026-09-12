/**
 * The prior-authorization status dashboard (TASK-072).
 *
 * A provider sees every authorization request from their own visits, what each
 * one is waiting on, why a denied one was refused, and — for the two states
 * where asking again is the right move — a way to send it again.
 *
 * Four things here are requirements rather than presentation choices.
 *
 * **Manual submission is its own state, not a failure and not a pending payer
 * decision.** `queueEntryView` owns that distinction; this screen only renders
 * what it says. Most commercial plans are outside the CMS-0057-F mandate, so it
 * is the ordinary outcome rather than an exception.
 *
 * **Resubmission is offered for `denied` and `error` only**, and the condition
 * is `canResubmit` rather than anything derived here. A request the payer is
 * still holding must not be asked again — it would open a second review of one
 * request — and the endpoint answers 409 for exactly that.
 *
 * **A denial reason is fetched when it is asked for.** It is the payer's account
 * of why this patient's care was refused, so reading it is an audited PHI
 * access. Loading one for every denied row on every page load would write an
 * audit row per refresh and bury the accesses an audit is actually asked about.
 *
 * **A resubmission's outcome is reported where it happened**, including the
 * reason a request was handed to a person, which the router returns but does not
 * persist — so it is visible now and gone on reload. TASK-072b is that gap.
 *
 * Nothing here logs. A denial reason is clinical content, and every row names a
 * visit.
 */

import type { ApiFailure } from '@medauth/session-client';
import { useCallback, useState } from 'react';

import {
  priorAuthApi,
  type PriorAuthApi,
  type PriorAuthDecision,
  type PriorAuthSummary,
  type ResubmitOutcome,
} from '../api/priorAuth';
import { usePriorAuthQueue } from '../hooks/usePriorAuthQueue';
import { canResubmit, hasDenialReason, queueEntryView } from '../priorAuth/queue';

export interface PriorAuthDashboardProps {
  /**
   * Whose queue to show.
   *
   * A scope rather than a credential: the list route requires it because an
   * unscoped queue would span every patient and provider. It comes from the
   * launch context, already resolved, so this app never handles a practitioner
   * reference.
   */
  providerId: string;
  api?: PriorAuthApi;
  /** Offered as a way back to the visit flow. */
  onStartAnotherVisit?: () => void;
}

/** What a row's denial reason is doing, if anything. */
type ReasonState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'loaded'; decision: PriorAuthDecision }
  | { kind: 'error'; failure: ApiFailure };

/** What a row's resubmission is doing, if anything. */
type ResubmitState =
  | { kind: 'idle' }
  | { kind: 'sending' }
  | { kind: 'done'; outcome: ResubmitOutcome }
  | { kind: 'error'; failure: ApiFailure };

const CHIP_CLASSES: Record<string, string> = {
  pending: 'bg-slate-100 text-slate-700',
  'with-payer': 'bg-blue-100 text-blue-800',
  decided: 'bg-emerald-100 text-emerald-800',
  failed: 'bg-red-100 text-red-800',
  // Deliberately not the failure colour: this is work to do, not something
  // broken, and colour is the first thing read.
  manual: 'bg-amber-100 text-amber-900',
  unknown: 'bg-slate-100 text-slate-700',
};

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString();
}

export function PriorAuthDashboard({
  providerId,
  api = priorAuthApi,
  onStartAnotherVisit,
}: PriorAuthDashboardProps) {
  const { state, loadMore, refresh, replace } = usePriorAuthQueue(providerId, { api });
  const [reasons, setReasons] = useState<Record<string, ReasonState>>({});
  const [resubmits, setResubmits] = useState<Record<string, ResubmitState>>({});

  const showReason = useCallback(
    (requestId: string) => {
      setReasons((current) => ({ ...current, [requestId]: { kind: 'loading' } }));
      void (async () => {
        const result = await api.readDecision(requestId);
        setReasons((current) => ({
          ...current,
          [requestId]: result.ok
            ? { kind: 'loaded', decision: result.value }
            : { kind: 'error', failure: result.failure },
        }));
      })();
    },
    [api],
  );

  const resubmit = useCallback(
    (request: PriorAuthSummary) => {
      setResubmits((current) => ({ ...current, [request.requestId]: { kind: 'sending' } }));
      void (async () => {
        const result = await api.resubmit(request.requestId);
        setResubmits((current) => ({
          ...current,
          [request.requestId]: result.ok
            ? { kind: 'done', outcome: result.value }
            : { kind: 'error', failure: result.failure },
        }));
        if (result.ok) {
          // The row's status has moved, and this screen holds a stale copy of
          // it. Re-reading the page would also work and would cost a request per
          // resubmission; the row is updated from what the router just said.
          replace({
            ...request,
            rawStatus: result.value.outcome,
            status: result.value.outcome === 'submitted' ? 'submitted' : 'manual-submission-required',
            submissionMethod: result.value.submissionMethod,
            payerOutcome: result.value.payerOutcome,
            submittable: false,
          });
        }
      })();
    },
    [api, replace],
  );

  if (state.kind === 'loading') {
    return (
      <Frame onStartAnotherVisit={onStartAnotherVisit}>
        <p className="text-sm text-slate-700" data-testid="queue-loading">
          Loading prior authorizations…
        </p>
      </Frame>
    );
  }

  if (state.kind === 'error') {
    return (
      <Frame onStartAnotherVisit={onStartAnotherVisit}>
        <p className="text-sm text-red-700" data-testid="queue-error">
          {state.failure.message}
        </p>
        <button
          type="button"
          onClick={refresh}
          className="self-start rounded bg-slate-800 px-3 py-1.5 text-sm font-medium text-white"
        >
          Try again
        </button>
      </Frame>
    );
  }

  if (state.requests.length === 0) {
    return (
      <Frame onStartAnotherVisit={onStartAnotherVisit}>
        {/* Distinct from a failed load above: this says the queue was read and
            is empty, rather than leaving one blank panel to mean both. */}
        <p className="text-sm text-slate-700" data-testid="queue-empty">
          No prior authorization requests from your visits yet.
        </p>
      </Frame>
    );
  }

  return (
    <Frame onStartAnotherVisit={onStartAnotherVisit}>
      <ul className="flex flex-col gap-3" data-testid="queue-list">
        {state.requests.map((request) => {
          const view = queueEntryView(request);
          const reason = reasons[request.requestId] ?? { kind: 'idle' };
          const sending = resubmits[request.requestId] ?? { kind: 'idle' };

          return (
            <li
              key={request.requestId}
              data-testid="queue-row"
              data-status={request.rawStatus}
              className="flex flex-col gap-2 rounded border border-slate-200 p-4"
            >
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-sm font-medium text-slate-900">
                  {request.payerName ?? 'No payer recorded'}
                </span>
                <span
                  data-testid="queue-status"
                  data-kind={view.kind}
                  className={`rounded px-2 py-0.5 text-xs font-medium ${CHIP_CLASSES[view.kind]}`}
                >
                  {view.label}
                </span>
              </div>

              <p className="text-sm text-slate-700">{view.detail}</p>
              <p className="text-xs text-slate-500">Visit of {formatDate(request.startedAt)}</p>

              {hasDenialReason(request) && (
                <DenialReason
                  state={reason}
                  onShow={() => showReason(request.requestId)}
                />
              )}

              {canResubmit(request) && sending.kind !== 'done' && (
                <button
                  type="button"
                  data-testid="resubmit"
                  disabled={sending.kind === 'sending'}
                  onClick={() => resubmit(request)}
                  className="self-start rounded bg-slate-800 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-60"
                >
                  {sending.kind === 'sending' ? 'Sending…' : 'Send to payer again'}
                </button>
              )}

              {sending.kind === 'error' && (
                <p className="text-sm text-red-700" data-testid="resubmit-error">
                  {sending.failure.message}
                </p>
              )}

              {sending.kind === 'done' && (
                <p className="text-sm text-slate-700" data-testid="resubmit-done">
                  {sending.outcome.outcome === 'submitted'
                    ? 'Sent to the payer again.'
                    : `Nothing was transmitted — this one has to be submitted by hand${
                        sending.outcome.reason === null ? '' : ` (${sending.outcome.reason})`
                      }.`}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      {state.nextCursor !== null && (
        <button
          type="button"
          data-testid="load-more"
          disabled={state.loadingMore}
          onClick={loadMore}
          className="self-start rounded border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-800 disabled:opacity-60"
        >
          {state.loadingMore ? 'Loading…' : 'Show more'}
        </button>
      )}
    </Frame>
  );
}

/**
 * The denial reason, behind a control.
 *
 * Not fetched with the row: reading it is an audited PHI access, and one audit
 * row per provider actually opening a denial is worth more than one per page
 * load. A payer that denied without giving a reason is reported as exactly that
 * — it is a different fact from a request that was never denied, and rendering
 * nothing would collapse the two.
 */
function DenialReason({ state, onShow }: { state: ReasonState; onShow: () => void }) {
  if (state.kind === 'idle') {
    return (
      <button
        type="button"
        data-testid="show-denial-reason"
        onClick={onShow}
        className="self-start text-sm font-medium text-blue-700 underline"
      >
        Why was this denied?
      </button>
    );
  }

  if (state.kind === 'loading') {
    return (
      <p className="text-sm text-slate-600" data-testid="denial-reason-loading">
        Loading the payer's reason…
      </p>
    );
  }

  if (state.kind === 'error') {
    return (
      <p className="text-sm text-red-700" data-testid="denial-reason-error">
        {state.failure.message}
      </p>
    );
  }

  return (
    <p className="text-sm text-slate-800" data-testid="denial-reason">
      {state.decision.denialReason ?? 'The payer gave no reason for this denial.'}
    </p>
  );
}

function Frame({
  children,
  onStartAnotherVisit,
}: {
  children: React.ReactNode;
  onStartAnotherVisit?: (() => void) | undefined;
}) {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
      <div className="flex items-baseline justify-between gap-3">
        <h1 className="text-lg font-semibold text-slate-900">Prior authorizations</h1>
        {onStartAnotherVisit !== undefined && (
          <button
            type="button"
            onClick={onStartAnotherVisit}
            className="text-sm font-medium text-blue-700 underline"
          >
            Start a visit
          </button>
        )}
      </div>
      {children}
    </main>
  );
}
