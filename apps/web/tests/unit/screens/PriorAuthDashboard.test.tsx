/**
 * The prior-authorization dashboard (TASK-072).
 *
 * Three of these are the task's own acceptance criteria, and each is a
 * distinction that fails silently when it is got wrong: a denial reason offered
 * on something that was not denied, a resubmit control on a request the payer is
 * still holding, and a request needing a person rendered as either a failure or
 * a pending payer decision.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PriorAuthDashboard } from '../../../src/screens/PriorAuthDashboard';
import {
  aDecision,
  aDeniedRequest,
  aRequest,
  PROVIDER_ID,
  priorAuthFailing,
  priorAuthServing,
  type FakePriorAuth,
} from '../../support/priorAuth';

function renderDashboard(api: FakePriorAuth) {
  return render(<PriorAuthDashboard providerId={PROVIDER_ID} api={api} />);
}

describe('the queue', () => {
  it('asks only for this provider’s requests', async () => {
    const api = priorAuthServing();

    renderDashboard(api);

    await screen.findByTestId('queue-list');
    expect(api.listRequests).toHaveBeenCalledWith(PROVIDER_ID, {});
  });

  it('renders a row per request', async () => {
    const api = priorAuthServing({
      pages: [
        {
          requests: [
            aRequest(),
            aDeniedRequest(),
            aRequest({ requestId: 'r3', status: 'submitted', rawStatus: 'submitted' }),
          ],
          nextCursor: null,
        },
      ],
    });

    renderDashboard(api);

    expect(await screen.findAllByTestId('queue-row')).toHaveLength(3);
  });

  it('says an empty queue was read, rather than leaving a blank panel', async () => {
    const api = priorAuthServing({ pages: [{ requests: [], nextCursor: null }] });

    renderDashboard(api);

    expect(await screen.findByTestId('queue-empty')).toBeInTheDocument();
  });

  it('reports a failed load as a failure rather than as an empty queue', async () => {
    const api = priorAuthFailing(502, 'upstream', 'The server could not be reached.');

    renderDashboard(api);

    expect(await screen.findByTestId('queue-error')).toHaveTextContent(
      'The server could not be reached.',
    );
    expect(screen.queryByTestId('queue-empty')).not.toBeInTheDocument();
  });

  it('shows a row whose status it does not recognise rather than dropping it', async () => {
    // The status column is free text by design so a payer-specific state can be
    // added without a migration. A queue that hid what it did not know would
    // under-report a provider's outstanding work.
    const api = priorAuthServing({
      pages: [
        { requests: [aRequest({ status: 'other', rawStatus: 'awaiting-peer-review' })], nextCursor: null },
      ],
    });

    renderDashboard(api);

    expect(await screen.findByTestId('queue-status')).toHaveTextContent('awaiting-peer-review');
  });
});

describe('the denial reason', () => {
  it('is offered only on a denied request', async () => {
    const api = priorAuthServing({
      pages: [
        {
          requests: [
            aRequest(),
            aRequest({ requestId: 'r2', status: 'submitted', rawStatus: 'submitted' }),
            aRequest({ requestId: 'r3', status: 'approved', rawStatus: 'approved' }),
            aRequest({ requestId: 'r4', status: 'error', rawStatus: 'error' }),
            aDeniedRequest(),
          ],
          nextCursor: null,
        },
      ],
    });

    renderDashboard(api);

    await screen.findByTestId('queue-list');
    expect(screen.getAllByTestId('show-denial-reason')).toHaveLength(1);
  });

  it('is not read until it is asked for', async () => {
    // Reading it is an audited PHI access, so one row per provider opening a
    // denial is the point — not one per page load.
    const api = priorAuthServing({ pages: [{ requests: [aDeniedRequest()], nextCursor: null }] });

    renderDashboard(api);

    await screen.findByTestId('show-denial-reason');
    expect(api.readDecision).not.toHaveBeenCalled();
  });

  it('shows the payer’s words once asked for', async () => {
    const api = priorAuthServing({ pages: [{ requests: [aDeniedRequest()], nextCursor: null }] });

    renderDashboard(api);
    fireEvent.click(await screen.findByTestId('show-denial-reason'));

    expect(await screen.findByTestId('denial-reason')).toHaveTextContent(
      'Six weeks of conservative therapy not documented.',
    );
  });

  it('says a denial carried no reason rather than showing nothing', async () => {
    // A payer that denied without saying why is a different fact from a request
    // that was never denied, and an empty panel would collapse the two.
    const api = priorAuthServing({
      pages: [{ requests: [aDeniedRequest()], nextCursor: null }],
      decision: aDecision({ denialReason: null }),
    });

    renderDashboard(api);
    fireEvent.click(await screen.findByTestId('show-denial-reason'));

    expect(await screen.findByTestId('denial-reason')).toHaveTextContent('gave no reason');
  });
});

describe('resubmission', () => {
  it('is offered for denied and error, and for no other status', async () => {
    const api = priorAuthServing({
      pages: [
        {
          requests: [
            aRequest(),
            aRequest({ requestId: 'r2', status: 'submitted', rawStatus: 'submitted' }),
            aRequest({ requestId: 'r3', status: 'approved', rawStatus: 'approved' }),
            aRequest({
              requestId: 'r4',
              status: 'manual-submission-required',
              rawStatus: 'manual-submission-required',
            }),
            aRequest({ requestId: 'r5', status: 'error', rawStatus: 'error' }),
            aDeniedRequest(),
          ],
          nextCursor: null,
        },
      ],
    });

    renderDashboard(api);

    await screen.findByTestId('queue-list');
    expect(screen.getAllByTestId('resubmit')).toHaveLength(2);
  });

  it('is withheld when the service says the request is not submittable', async () => {
    // `submittable` is computed where the row lives, and a denied request the
    // service would refuse is not a control worth offering. The alternative is a
    // button whose only outcome is a 409.
    const api = priorAuthServing({
      pages: [{ requests: [aDeniedRequest({ submittable: false })], nextCursor: null }],
    });

    renderDashboard(api);

    await screen.findByTestId('queue-row');
    expect(screen.queryByTestId('resubmit')).not.toBeInTheDocument();
  });

  it('sends the request again and reports that it went to the payer', async () => {
    const api = priorAuthServing({
      pages: [{ requests: [aDeniedRequest()], nextCursor: null }],
    });

    renderDashboard(api);
    fireEvent.click(await screen.findByTestId('resubmit'));

    expect(await screen.findByTestId('resubmit-done')).toHaveTextContent('Sent to the payer again');
    expect(api.resubmit).toHaveBeenCalledWith('66666666-6666-4666-8666-666666666666');
  });

  it('reports a routed-to-a-person outcome as work rather than as a failure', async () => {
    // The manual path is the ordinary case for a commercial plan, and the reason
    // is available here and nowhere else: the router returns it without
    // persisting it (TASK-072b).
    const api = priorAuthServing({
      pages: [{ requests: [aDeniedRequest()], nextCursor: null }],
      resubmit: {
        ok: true,
        value: {
          outcome: 'manual-submission-required',
          payerOutcome: null,
          submissionMethod: null,
          reason: 'payer-has-no-prior-auth-api',
        },
      },
    });

    renderDashboard(api);
    fireEvent.click(await screen.findByTestId('resubmit'));

    const done = await screen.findByTestId('resubmit-done');
    expect(done).toHaveTextContent('submitted by hand');
    expect(done).toHaveTextContent('payer-has-no-prior-auth-api');
    expect(screen.queryByTestId('resubmit-error')).not.toBeInTheDocument();
  });

  it('reports a refusal against the row it was pressed on', async () => {
    const api = priorAuthServing({
      pages: [{ requests: [aDeniedRequest()], nextCursor: null }],
      resubmit: {
        ok: false,
        failure: {
          kind: 'status',
          status: 409,
          code: 'prior_auth_not_submittable',
          message: 'The payer is still holding this request.',
        },
      },
    });

    renderDashboard(api);
    fireEvent.click(await screen.findByTestId('resubmit'));

    expect(await screen.findByTestId('resubmit-error')).toHaveTextContent(
      'The payer is still holding this request.',
    );
  });

  it('does not offer the same request twice once it has been sent', async () => {
    const api = priorAuthServing({
      pages: [{ requests: [aDeniedRequest()], nextCursor: null }],
    });

    renderDashboard(api);
    fireEvent.click(await screen.findByTestId('resubmit'));
    await screen.findByTestId('resubmit-done');

    expect(screen.queryByTestId('resubmit')).not.toBeInTheDocument();
  });
});

describe('a request needing manual submission', () => {
  it('reads as work for a person, not as a failure and not as pending', async () => {
    const api = priorAuthServing({
      pages: [
        {
          requests: [
            aRequest({
              status: 'manual-submission-required',
              rawStatus: 'manual-submission-required',
            }),
          ],
          nextCursor: null,
        },
      ],
    });

    renderDashboard(api);

    const chip = await screen.findByTestId('queue-status');
    // Its own kind, which is what keeps it out of both the failure styling and
    // the "waiting on the payer" wording.
    expect(chip).toHaveAttribute('data-kind', 'manual');
    expect(chip).not.toHaveAttribute('data-kind', 'failed');
    expect(screen.getByTestId('queue-row')).toHaveTextContent('has to submit this request');
    expect(screen.getByTestId('queue-row')).not.toHaveTextContent('has not decided');
  });

  it('is told apart from a payer that refused to take the request in', async () => {
    const api = priorAuthServing({
      pages: [
        {
          requests: [
            aRequest({
              requestId: 'manual',
              status: 'manual-submission-required',
              rawStatus: 'manual-submission-required',
            }),
            aRequest({ requestId: 'errored', status: 'error', rawStatus: 'error' }),
          ],
          nextCursor: null,
        },
      ],
    });

    renderDashboard(api);

    const [manual, errored] = await screen.findAllByTestId('queue-status');
    expect(manual).toHaveAttribute('data-kind', 'manual');
    expect(errored).toHaveAttribute('data-kind', 'failed');
  });
});

describe('paging', () => {
  it('appends the next page rather than replacing what is shown', async () => {
    const api = priorAuthServing({
      pages: [
        { requests: [aRequest({ requestId: 'first' })], nextCursor: 'cursor-1' },
        { requests: [aRequest({ requestId: 'second' })], nextCursor: null },
      ],
    });

    renderDashboard(api);
    fireEvent.click(await screen.findByTestId('load-more'));

    await waitFor(() => expect(screen.getAllByTestId('queue-row')).toHaveLength(2));
    expect(api.listRequests).toHaveBeenLastCalledWith(PROVIDER_ID, { cursor: 'cursor-1' });
  });

  it('offers no control when the first page is the last', async () => {
    const api = priorAuthServing();

    renderDashboard(api);

    await screen.findByTestId('queue-list');
    expect(screen.queryByTestId('load-more')).not.toBeInTheDocument();
  });
});
