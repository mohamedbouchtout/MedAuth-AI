/**
 * Client for the prior-authorization dashboard (TASK-072).
 *
 * **Three calls across two services, and which service answers which is not
 * arbitrary.** The queue and a denial reason come from `API_BASE_URL` —
 * track-a-clinical owns `prior_auth_requests` and every read of it — and a
 * resubmission goes to `PRIOR_AUTH_URL`, because choosing a submission path is
 * what `prior-auth` exists to do. Two origins, declared separately, for the same
 * reason this app already holds four: they are different services on different
 * ports until the Phase 6 gateway CLAUDE.md defers to.
 *
 * **This stays in `apps/web` rather than becoming a package**, on the same terms
 * as `./notes` and the transcript parser: there is one consumer, `apps/mobile`
 * has no dashboard, and the established trigger for extraction is a second one.
 *
 * **No credential, deliberately.** All three routes are unauthenticated in v1
 * and the audit actor is the provider on the `encounters` row, never a claim
 * this app presents. What the dashboard does send is `provider_id` as a query
 * parameter, which is a *scope* rather than a credential — the list is refused
 * without one because it would otherwise span every patient and provider.
 *
 * **Nothing here logs.** A denial reason is a payer's account of why a patient's
 * care was refused, and every request URL names either a provider or a request.
 */

import type { ApiFailure, ApiResult } from '@medauth/session-client';

import { API_BASE_URL, PRIOR_AUTH_URL } from '../config';

/**
 * Where a request has got to in our own process.
 *
 * Narrowed from a free-text column rather than an enum — the server's own
 * comment says a payer-specific state must be addable without a migration — so
 * `other` carries anything this app has not been taught. It is rendered as the
 * raw status rather than hidden: a queue that silently dropped rows it did not
 * recognise would under-report a provider's outstanding work.
 */
export type PriorAuthStatus =
  | 'pending'
  | 'submitted'
  | 'approved'
  | 'denied'
  | 'error'
  | 'manual-submission-required';

/**
 * One row of the provider's queue.
 *
 * **No clinical field, and that is the server's constraint rather than this
 * app's choice.** The list route writes no audit row precisely because nothing
 * it returns is clinical, so a denial reason is fetched one request at a time
 * through `readDecision`. See CLAUDE.md's audit rule and the `PriorAuthListItem`
 * schema, which states it where a field would be added.
 */
export interface PriorAuthSummary {
  requestId: string;
  /** The visit it came out of. What a link to the note is keyed on. */
  sessionId: string;
  /** Free text on the wire; `status` below is the narrowed form. */
  rawStatus: string;
  status: PriorAuthStatus | 'other';
  payerName: string | null;
  payerOutcome: string | null;
  submissionMethod: string | null;
  submittedAt: string | null;
  decidedAt: string | null;
  /** When the visit began. The row's date — the request carries none of its own. */
  startedAt: string;
  /**
   * Whether a submission may be attempted now.
   *
   * Computed by the service that owns the row and **never re-derived here**: the
   * rule is "never submitted, or terminal and unsuccessful", and a client that
   * checked `submittedAt` instead would hide the resubmit control on every
   * request that actually needs it — which is the defect TASK-061 found in
   * `fhir-integration`.
   */
  submittable: boolean;
}

/** One page of the queue, and how to ask for the next. */
export interface PriorAuthPage {
  requests: PriorAuthSummary[];
  /** Opaque. Passed back verbatim; this app never parses it. */
  nextCursor: string | null;
}

/** What the payer decided about one request, and why. */
export interface PriorAuthDecision {
  requestId: string;
  payerOutcome: string | null;
  payerReferenceNumber: string | null;
  decidedAt: string | null;
  /**
   * Why the request was refused, in the payer's own words.
   *
   * `null` on a denial means the payer gave no reason — a different fact from a
   * request that was never denied, and the screen must not render either as the
   * other.
   */
  denialReason: string | null;
}

/** What a resubmission did: transmitted to the payer, or handed to a person. */
export interface ResubmitOutcome {
  /**
   * `submitted` or `manual-submission-required`, as the router reported it.
   *
   * The second is not a failure — most commercial plans are outside the
   * CMS-0057-F mandate, so it is the ordinary case — and it is rendered as work
   * for a person rather than as an error or a pending payer decision.
   */
  outcome: string;
  /** What the payer said, when it was asked. Null on the manual path. */
  payerOutcome: string | null;
  /** Which path transmitted it, as the adapter reported. Null on the manual path. */
  submissionMethod: string | null;
  /**
   * Why a person has to submit this, on the manual path — a fixed label.
   *
   * **Available here and nowhere else, which is a real limitation rather than an
   * oversight.** The router logs this label and returns it, but does not persist
   * it, so it survives only as long as this response: reload the dashboard and
   * the row says `manual-submission-required` with no reason attached.
   * Persisting it needs a column and a closed vocabulary, which is TASK-072b.
   */
  reason: string | null;
}

export interface PriorAuthApi {
  listRequests(
    providerId: string,
    options?: { status?: string; cursor?: string },
  ): Promise<ApiResult<PriorAuthPage>>;
  readDecision(requestId: string): Promise<ApiResult<PriorAuthDecision>>;
  resubmit(requestId: string): Promise<ApiResult<ResubmitOutcome>>;
}

/** The subset of `fetch` this client uses, so tests can supply their own. */
export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

const MALFORMED: ApiFailure = {
  kind: 'malformed',
  message: 'The server returned a response MedAuth AI could not read.',
};

function networkFailure(): ApiFailure {
  // The thrown value is never surfaced: it can carry the request URL, which
  // names a provider or a request, and a provider can act on "unreachable" but
  // not on a stack.
  return {
    kind: 'network',
    message: 'MedAuth AI could not reach the server. Check the network connection.',
  };
}

function readError(body: unknown, status: number): ApiFailure {
  const error = (body as { error?: unknown } | null)?.error;
  if (typeof error === 'object' && error !== null) {
    const { code, message } = error as { code?: unknown; message?: unknown };
    if (typeof code === 'string' && typeof message === 'string') {
      return { kind: 'status', status, code, message };
    }
  }
  return { kind: 'status', status, code: 'unknown', message: `The server returned ${status}.` };
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

const KNOWN_STATUSES: readonly PriorAuthStatus[] = [
  'pending',
  'submitted',
  'approved',
  'denied',
  'error',
  'manual-submission-required',
];

/** Narrow a status, keeping anything unrecognised rather than discarding the row. */
export function narrowStatus(raw: string): PriorAuthStatus | 'other' {
  return KNOWN_STATUSES.includes(raw as PriorAuthStatus) ? (raw as PriorAuthStatus) : 'other';
}

function readSummary(value: unknown): PriorAuthSummary | null {
  if (typeof value !== 'object' || value === null) {
    return null;
  }
  const fields = value as Record<string, unknown>;
  const requestId = fields.request_id;
  const sessionId = fields.session_id;
  const rawStatus = fields.status;
  const startedAt = fields.started_at;
  const submittable = fields.submittable;
  if (
    typeof requestId !== 'string' ||
    typeof sessionId !== 'string' ||
    typeof rawStatus !== 'string' ||
    typeof startedAt !== 'string' ||
    typeof submittable !== 'boolean'
  ) {
    return null;
  }
  return {
    requestId,
    sessionId,
    rawStatus,
    status: narrowStatus(rawStatus),
    payerName: optionalString(fields.payer_name),
    payerOutcome: optionalString(fields.payer_outcome),
    submissionMethod: optionalString(fields.submission_method),
    submittedAt: optionalString(fields.submitted_at),
    decidedAt: optionalString(fields.decided_at),
    startedAt,
    submittable,
  };
}

function readPage(body: unknown): PriorAuthPage | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const { requests, next_cursor: nextCursor } = data as Record<string, unknown>;
  if (!Array.isArray(requests)) {
    return null;
  }
  const rows: PriorAuthSummary[] = [];
  for (const entry of requests) {
    const row = readSummary(entry);
    if (row === null) {
      return null;
    }
    rows.push(row);
  }
  return { requests: rows, nextCursor: optionalString(nextCursor) };
}

function readDecisionBody(body: unknown): PriorAuthDecision | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const fields = data as Record<string, unknown>;
  if (typeof fields.request_id !== 'string') {
    return null;
  }
  return {
    requestId: fields.request_id,
    payerOutcome: optionalString(fields.payer_outcome),
    payerReferenceNumber: optionalString(fields.payer_reference_number),
    decidedAt: optionalString(fields.decided_at),
    denialReason: optionalString(fields.denial_reason),
  };
}

function readResubmitBody(body: unknown): ResubmitOutcome | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const fields = data as Record<string, unknown>;
  if (typeof fields.outcome !== 'string') {
    return null;
  }
  return {
    outcome: fields.outcome,
    payerOutcome: optionalString(fields.payer_outcome),
    submissionMethod: optionalString(fields.submission_method),
    reason: optionalString(fields.reason),
  };
}

export function createPriorAuthApi(
  options: { clinicalBaseUrl?: string; priorAuthBaseUrl?: string; fetchImpl?: FetchLike } = {},
): PriorAuthApi {
  const {
    clinicalBaseUrl = API_BASE_URL,
    priorAuthBaseUrl = PRIOR_AUTH_URL,
    fetchImpl = (url, init) => fetch(url, init),
  } = options;

  async function call<T>(
    url: string,
    read: (body: unknown) => T | null,
    init?: RequestInit,
  ): Promise<ApiResult<T>> {
    let response: Response;
    try {
      response = await fetchImpl(url, init);
    } catch {
      return { ok: false, failure: networkFailure() };
    }

    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // Only fatal on the success path: an error status still says what
      // happened without a parseable body, which is what a proxy 502 looks like.
      if (response.ok) {
        return { ok: false, failure: MALFORMED };
      }
    }

    if (!response.ok) {
      return { ok: false, failure: readError(body, response.status) };
    }

    const value = read(body);
    return value === null ? { ok: false, failure: MALFORMED } : { ok: true, value };
  }

  return {
    listRequests(providerId, { status, cursor } = {}) {
      // `provider_id` is always sent. The server requires it, and that is the
      // point: an unscoped queue would span every patient and provider.
      const query = new URLSearchParams({ provider_id: providerId });
      if (status !== undefined) {
        query.set('status', status);
      }
      if (cursor !== undefined) {
        query.set('cursor', cursor);
      }
      return call(`${clinicalBaseUrl}/prior-auth?${query.toString()}`, readPage);
    },

    readDecision(requestId) {
      return call(`${clinicalBaseUrl}/prior-auth/${requestId}/decision`, readDecisionBody);
    },

    resubmit(requestId) {
      return call(`${priorAuthBaseUrl}/prior-auth/${requestId}/submit`, readResubmitBody, {
        method: 'POST',
      });
    },
  };
}

/** The client the dashboard uses when none is injected. */
export const priorAuthApi = createPriorAuthApi();
