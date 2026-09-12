/**
 * The `/prior-auth` route's one job: establish who is asking (TASK-072).
 *
 * The queue is scoped to a provider and this app never asserts a provider
 * identity of its own, so the identity comes from the launch context. The two
 * ways that can fail are both ordinary states rather than errors, and the rule
 * they follow is the one this repository applies everywhere: say what is true
 * instead of rendering something that can only mislead.
 *
 * An empty list is the specific wrong answer being guarded against here. It
 * would tell a provider they have no outstanding authorizations, which is a
 * claim about their work rather than about this app's sign-in state, and it
 * might be false.
 */

import type { ApiResult } from '@medauth/session-client';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { FhirApi, LaunchContext } from '@medauth/fhir-client';

import { PriorAuthRoute } from '../../../src/screens/PriorAuthRoute';
import { priorAuthServing, PROVIDER_ID } from '../../support/priorAuth';

const LAUNCH_ID = 'launch-1';

function fhirServing(context: Partial<LaunchContext>): FhirApi {
  const value: LaunchContext = {
    patientId: 'Patient/1',
    encounterId: 'Encounter/1',
    providerId: PROVIDER_ID,
    ...context,
  } as LaunchContext;
  return {
    getLaunchContext: vi.fn(() => Promise.resolve({ ok: true as const, value })),
  } as unknown as FhirApi;
}

describe('before the queue', () => {
  it('resolves the provider from the launch and shows the queue', async () => {
    const api = priorAuthServing();

    render(
      <PriorAuthRoute launchId={LAUNCH_ID} fhir={fhirServing({})} api={api} />,
    );

    await screen.findByTestId('queue-list');
    expect(api.listRequests).toHaveBeenCalledWith(PROVIDER_ID, {});
  });

  it('takes the provider from a launch that named no patient', async () => {
    // A standalone launch carries a provider and no patient. Which patient this
    // visit is about says nothing about whose queue this is.
    const api = priorAuthServing();

    render(
      <PriorAuthRoute
        launchId={LAUNCH_ID}
        fhir={fhirServing({ patientId: null, encounterId: null })}
        api={api}
      />,
    );

    await screen.findByTestId('queue-list');
    expect(api.listRequests).toHaveBeenCalledWith(PROVIDER_ID, {});
  });

  it('says why it cannot show a queue with no launch, and asks for nothing', async () => {
    const api = priorAuthServing();

    render(<PriorAuthRoute launchId={null} fhir={fhirServing({})} api={api} />);

    expect(await screen.findByTestId('queue-unavailable')).toHaveTextContent(
      'Sign in from your EHR',
    );
    expect(screen.queryByTestId('queue-empty')).not.toBeInTheDocument();
    expect(api.listRequests).not.toHaveBeenCalled();
  });

  it('says so when the EHR never proved who launched us', async () => {
    // TASK-051c records a practitioner only after verifying the id_token, so a
    // null provider means we do not know who is asking — and there is no query
    // to make. One wording for one situation, shared with the patient picker.
    const api = priorAuthServing();

    render(
      <PriorAuthRoute
        launchId={LAUNCH_ID}
        fhir={fhirServing({ providerId: null })}
        api={api}
      />,
    );

    await screen.findByTestId('queue-unavailable');
    expect(api.listRequests).not.toHaveBeenCalled();
  });

  it('reports a launch context that could not be read', async () => {
    const failure: ApiResult<LaunchContext> = {
      ok: false,
      failure: { kind: 'status', status: 401, code: 'launch_expired', message: 'Launch again.' },
    };
    const fhir = {
      getLaunchContext: vi.fn(() => Promise.resolve(failure)),
    } as unknown as FhirApi;

    render(<PriorAuthRoute launchId={LAUNCH_ID} fhir={fhir} api={priorAuthServing()} />);

    expect(await screen.findByTestId('queue-unavailable')).toHaveTextContent('Launch again.');
  });
});
