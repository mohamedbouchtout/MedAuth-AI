import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { FhirApi, LaunchContext } from '@medauth/fhir-client';

import type { ApiResult, Session, SessionsApi } from '../../../src/api/sessions';
import { VisitFlow } from '../../../src/screens/VisitFlow';
import { installFakeWebSocket, tokenExpiringAt } from '../../support/transcript';

/**
 * The order a visit passes through (TASK-070).
 *
 * The picker decides who the visit is about and the session screen then records
 * it. That order is the substance rather than navigation: the session screen
 * takes a decided subject, and a flow that reached it without one would be
 * offering to record an encounter against nobody.
 */

const LAUNCH_ID = 'launch-7';
const PROVIDER_ID = '22222222-2222-4222-8222-222222222222';
const NOW = 1_700_000_000_000;

function fhirWith(context: LaunchContext): FhirApi {
  return {
    getLaunchContext: vi.fn(async () => ({ ok: true as const, value: context })),
    searchPatients: vi.fn(),
  };
}

function sessions(): SessionsApi {
  return {
    startVisit: vi.fn(
      async (): Promise<ApiResult<Session>> => ({
        ok: true,
        value: { sessionId: 'session-1', jwt: tokenExpiringAt(NOW, 900) },
      }),
    ),
    remintToken: vi.fn(),
    endVisit: vi.fn(),
  } as unknown as SessionsApi;
}

describe('the order of the two screens', () => {
  it('shows the picker until a subject is decided', () => {
    installFakeWebSocket();
    render(
      <VisitFlow
        launchId={LAUNCH_ID}
        fhir={fhirWith({ patientId: null, encounterId: null, providerId: PROVIDER_ID })}
        sessions={sessions()}
      />,
    );

    expect(screen.getByTestId('patient-picker')).toBeInTheDocument();
    expect(screen.queryByTestId('start-visit')).not.toBeInTheDocument();
  });

  it('moves to the session screen once the EHR launch names a patient', async () => {
    installFakeWebSocket();
    render(
      <VisitFlow
        launchId={LAUNCH_ID}
        fhir={fhirWith({
          patientId: 'patient-1',
          encounterId: 'Encounter/7',
          providerId: PROVIDER_ID,
        })}
        sessions={sessions()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId('start-visit')).toBeInTheDocument());
  });

  /**
   * A visit cannot be started without a launch, and the refusal is the picker's
   * to make — this flow does not paper over it by rendering a session screen
   * that would then have nobody to record.
   */
  it('never reaches the session screen without a launch', () => {
    installFakeWebSocket();
    render(
      <VisitFlow
        launchId={null}
        fhir={fhirWith({ patientId: null, encounterId: null, providerId: null })}
        sessions={sessions()}
      />,
    );

    expect(screen.getByTestId('picker-failed')).toBeInTheDocument();
    expect(screen.queryByTestId('start-visit')).not.toBeInTheDocument();
  });
});
