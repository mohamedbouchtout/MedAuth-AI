/**
 * Prior-authorization fixtures for TASK-072's tests.
 *
 * The statuses here are the real ones from
 * `track_a_clinical.models.prior_auth_request` and the payloads are
 * `docs/api/track-a-clinical.yaml`'s, not shapes invented for the tests. The
 * distinctions worth exercising are the ones the contract actually draws —
 * `denied` against `error` against `manual-submission-required`, and a denial
 * with no reason against no denial at all — so a fixture that blurred them would
 * prove nothing.
 */

import type { ApiResult } from '@medauth/session-client';
import { vi, type Mock } from 'vitest';

import type {
  PriorAuthApi,
  PriorAuthDecision,
  PriorAuthPage,
  PriorAuthSummary,
  ResubmitOutcome,
} from '../../src/api/priorAuth';

export const PROVIDER_ID = '33333333-3333-4333-8333-333333333333';

export function aRequest(overrides: Partial<PriorAuthSummary> = {}): PriorAuthSummary {
  const rawStatus = overrides.rawStatus ?? overrides.status ?? 'pending';
  return {
    requestId: '44444444-4444-4444-8444-444444444444',
    sessionId: '55555555-5555-4555-8555-555555555555',
    rawStatus,
    status: 'pending',
    payerName: 'Aetna',
    payerOutcome: null,
    submissionMethod: null,
    submittedAt: null,
    decidedAt: null,
    startedAt: '2026-09-01T14:00:00Z',
    // True for `pending` and for the two resubmittable states, which is the
    // server's own rule. A fixture that hardcoded `false` would make the
    // resubmit assertions pass for the wrong reason.
    submittable: true,
    ...overrides,
  };
}

/** A denied request, as a provider following one up would see it. */
export function aDeniedRequest(overrides: Partial<PriorAuthSummary> = {}): PriorAuthSummary {
  return aRequest({
    requestId: '66666666-6666-4666-8666-666666666666',
    status: 'denied',
    rawStatus: 'denied',
    payerOutcome: 'complete',
    submissionMethod: 'fhir-pas',
    submittedAt: '2026-09-02T09:00:00Z',
    decidedAt: '2026-09-04T09:00:00Z',
    submittable: true,
    ...overrides,
  });
}

export interface FakePriorAuth extends PriorAuthApi {
  listRequests: Mock<PriorAuthApi['listRequests']>;
  readDecision: Mock<PriorAuthApi['readDecision']>;
  resubmit: Mock<PriorAuthApi['resubmit']>;
}

export function aDecision(overrides: Partial<PriorAuthDecision> = {}): PriorAuthDecision {
  return {
    requestId: '66666666-6666-4666-8666-666666666666',
    payerOutcome: 'complete',
    payerReferenceNumber: 'AUTH-88213',
    decidedAt: '2026-09-04T09:00:00Z',
    denialReason: 'Six weeks of conservative therapy not documented.',
    ...overrides,
  };
}

/**
 * A client serving one page, one decision and one resubmission outcome.
 *
 * Pages are served in order, so a test that presses "show more" gets the second
 * one — which is what makes a paging assertion about this client meaningful
 * rather than about a fixture that always answers the same thing.
 */
export function priorAuthServing(
  options: {
    pages?: PriorAuthPage[];
    decision?: PriorAuthDecision;
    resubmit?: ApiResult<ResubmitOutcome>;
  } = {},
): FakePriorAuth {
  const {
    pages = [{ requests: [aRequest()], nextCursor: null }],
    decision = aDecision(),
    resubmit = {
      ok: true as const,
      value: { outcome: 'submitted', payerOutcome: 'queued', submissionMethod: 'fhir-pas', reason: null },
    },
  } = options;

  let served = 0;
  return {
    listRequests: vi.fn<PriorAuthApi['listRequests']>(() => {
      const page = pages[Math.min(served, pages.length - 1)];
      served += 1;
      return Promise.resolve({ ok: true as const, value: page! });
    }),
    readDecision: vi.fn<PriorAuthApi['readDecision']>(() =>
      Promise.resolve({ ok: true as const, value: decision }),
    ),
    resubmit: vi.fn<PriorAuthApi['resubmit']>(() => Promise.resolve(resubmit)),
  };
}

/** A client whose list read fails with one envelope error. */
export function priorAuthFailing(status: number, code: string, message = 'nope'): FakePriorAuth {
  const failure = { kind: 'status' as const, status, code, message };
  return {
    listRequests: vi.fn<PriorAuthApi['listRequests']>(() =>
      Promise.resolve({ ok: false as const, failure }),
    ),
    readDecision: vi.fn<PriorAuthApi['readDecision']>(() =>
      Promise.resolve({ ok: false as const, failure }),
    ),
    resubmit: vi.fn<PriorAuthApi['resubmit']>(() =>
      Promise.resolve({ ok: false as const, failure }),
    ),
  };
}
