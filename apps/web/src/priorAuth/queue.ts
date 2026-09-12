/**
 * What a queue row means, and what may be done to it (TASK-072).
 *
 * Pure functions, kept out of the screen so the two decisions that actually
 * matter here can be tested without rendering anything:
 *
 * **A request needing manual submission is its own state.** TASK-061's router
 * flags a payer with no automated path rather than transmitting anything, and
 * the great majority of commercial employer-sponsored plans land there — it is
 * the ordinary case, not a failure. Showing it as an error would send a provider
 * looking for something to fix; showing it as pending would have them waiting
 * for a payer that was never asked. Both are silent, and both are the specific
 * misreading the whole routing model exists to prevent.
 *
 * **Resubmission is offered for `denied` and `error`, and for nothing else.**
 * `error` means the payer refused to take the request in, so nothing is pending
 * and asking again is the right move despite `submitted_at` being set.
 */

import type { PriorAuthSummary } from '../api/priorAuth';

/**
 * How a row should read to a provider.
 *
 * Deliberately not one-to-one with the wire status: `approved` and `denied` are
 * both decisions and share a kind, while `error` and `manual` are kept apart
 * from each other and from `pending` because a provider does something
 * different about each.
 */
export type QueueEntryKind =
  /** Assembled, not yet sent. Nothing is required of anyone. */
  | 'pending'
  /** With the payer, awaiting a decision. */
  | 'with-payer'
  /** The payer decided — approved or denied. */
  | 'decided'
  /** The payer refused to take it in. Nothing is pending. */
  | 'failed'
  /** No payer API can take it, so a person submits it. Work, not a failure. */
  | 'manual'
  /** A status this app has not been taught. Shown, never dropped. */
  | 'unknown';

export interface QueueEntryView {
  kind: QueueEntryKind;
  /** Short label for the row's status chip. */
  label: string;
  /** One sentence saying what is true and, where it applies, whose move it is. */
  detail: string;
}

const VIEWS: Record<string, QueueEntryView> = {
  pending: {
    kind: 'pending',
    label: 'Not yet submitted',
    detail: 'Assembled from the visit and waiting to be sent to the payer.',
  },
  submitted: {
    kind: 'with-payer',
    label: 'With the payer',
    detail: 'Submitted. The payer has not decided yet.',
  },
  approved: {
    kind: 'decided',
    label: 'Approved',
    detail: 'The payer authorized this request.',
  },
  denied: {
    kind: 'decided',
    label: 'Denied',
    detail: 'The payer refused this request. It can be corrected and sent again.',
  },
  error: {
    kind: 'failed',
    label: 'Not accepted',
    detail:
      'The payer would not take this request in, so nothing is pending with them. ' +
      'It can be sent again.',
  },
  'manual-submission-required': {
    kind: 'manual',
    label: 'Submit by hand',
    detail:
      'This payer has no automated submission path, so nothing was transmitted. ' +
      'Someone has to submit this request directly.',
  },
};

/**
 * Describe one row.
 *
 * An unrecognised status is reported as itself rather than hidden or guessed at:
 * the server's status column is free text by design so a payer-specific state
 * can be added without a migration, and a queue that dropped rows it did not
 * recognise would under-report a provider's outstanding work.
 */
export function queueEntryView(request: PriorAuthSummary): QueueEntryView {
  return (
    VIEWS[request.rawStatus] ?? {
      kind: 'unknown',
      label: request.rawStatus,
      detail: 'This request is in a state MedAuth AI does not recognise.',
    }
  );
}

/**
 * Whether to offer a resubmission for this row.
 *
 * Two conditions, and neither is redundant. The status test is TASK-072's rule:
 * `denied` and `error` are the terminal unsuccessful states, and a request the
 * payer is still holding must never be asked again. The `submittable` flag is
 * the server's own answer to the same question, computed where the row lives —
 * so this offers a control only when the service would accept it, rather than
 * re-deriving the rule from `submittedAt` the way `fhir-integration` did until
 * TASK-061, which refused every legitimate resubmission.
 */
export function canResubmit(request: PriorAuthSummary): boolean {
  const terminal = request.status === 'denied' || request.status === 'error';
  return terminal && request.submittable;
}

/**
 * Whether this row's denial reason is worth asking for.
 *
 * Only a denial has one, and reading it is an audited PHI access — the reason is
 * the payer's account of why this patient's care was refused. So it is fetched
 * when a provider asks for it rather than for every denied row on every page
 * load, which would write an audit row per refresh and bury the accesses an
 * audit is actually asked about.
 */
export function hasDenialReason(request: PriorAuthSummary): boolean {
  return request.status === 'denied';
}
