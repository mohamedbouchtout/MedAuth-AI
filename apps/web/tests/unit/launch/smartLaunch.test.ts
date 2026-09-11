import { describe, expect, it, vi } from 'vitest';

import type { LaunchApi, LaunchSession } from '../../../src/api/fhirClient';
import {
  REDEMPTION_FAILED_MESSAGE,
  beginLaunch,
  completeLaunch,
} from '../../../src/launch/smartLaunch';

/**
 * The two halves of a browser SMART launch (TASK-070).
 *
 * What is asserted here is mostly about what does *not* happen: the claim code
 * must not reach the navigation, a failed redemption must not surface the
 * service's own wording, and a launch must not be reported as anything other
 * than launched or failed. The success path itself is thin on purpose — the
 * request shaping it depends on lives in `@medauth/fhir-client` and is tested
 * there, against both deliveries.
 */

const ISS = 'https://ehr.example.com/fhir';
const SESSION: LaunchSession = {
  launchId: 'launch-7',
  ehrType: 'athena',
  expiresInSeconds: 3600,
};

function apiWith(overrides: Partial<LaunchApi> = {}): LaunchApi {
  return {
    authorizeUrl: ({ iss, launch }) =>
      `https://fhir.test/fhir/launch?iss=${iss}&delivery=web${launch === undefined ? '' : `&launch=${launch}`}`,
    redeemClaim: async () => ({ ok: true, value: SESSION }),
    ...overrides,
  };
}

describe('beginLaunch', () => {
  it('navigates to the authorize URL the client built', () => {
    const navigate = vi.fn();

    beginLaunch(apiWith(), { iss: ISS, launch: 'ctx-1' }, navigate);

    expect(navigate).toHaveBeenCalledTimes(1);
    const url = navigate.mock.calls[0]?.[0] as string;
    expect(url).toContain(`iss=${ISS}`);
    expect(url).toContain('delivery=web');
    expect(url).toContain('launch=ctx-1');
  });

  /**
   * A standalone launch sends no `launch` at all, and that absence is load
   * bearing — an empty one presents as an EHR launch carrying an empty context.
   * Asserted here as well as in the package because this is the call site that
   * decides whether the field is passed.
   */
  it('passes no launch context for a standalone launch', () => {
    const navigate = vi.fn();

    beginLaunch(apiWith(), { iss: ISS }, navigate);

    expect(navigate.mock.calls[0]?.[0]).not.toContain('launch=');
  });
});

describe('completeLaunch', () => {
  it('redeems the code and reports the launch', async () => {
    const redeemClaim = vi.fn(async () => ({ ok: true as const, value: SESSION }));

    const outcome = await completeLaunch(apiWith({ redeemClaim }), 'code-1');

    expect(redeemClaim).toHaveBeenCalledWith('code-1');
    expect(outcome).toEqual({ kind: 'launched', session: SESSION });
  });

  /**
   * The service answers unknown, expired and already-redeemed identically so a
   * caller probing codes learns nothing, and there is nothing a provider can do
   * differently about any of them. Surfacing the service's own message would
   * leak that distinction back out one failure at a time.
   */
  it('reports a refused redemption in this app’s own words', async () => {
    const outcome = await completeLaunch(
      apiWith({
        redeemClaim: async () => ({
          ok: false,
          failure: {
            kind: 'status',
            status: 404,
            code: 'FHIR_UNKNOWN_CLAIM',
            message: 'No such claim.',
          },
        }),
      }),
      'code-1',
    );

    expect(outcome).toEqual({ kind: 'failed', message: REDEMPTION_FAILED_MESSAGE });
  });

  it('reports a network failure as a failed launch, not as a missing one', async () => {
    const outcome = await completeLaunch(
      apiWith({
        redeemClaim: async () => ({
          ok: false,
          failure: { kind: 'network', message: 'unreachable' },
        }),
      }),
      'code-1',
    );

    expect(outcome.kind).toBe('failed');
  });
});
