import type { FetchLike } from '@medauth/session-client';

import { MOBILE_DELIVERY, createLaunchApi } from '../../../src/api/launch';

/**
 * The launch client (TASK-025c).
 *
 * Three things matter beyond parsing. The authorize URL must declare
 * `delivery=mobile`, or the callback answers JSON into a browser and this app
 * never sees the launch at all. The presence of `launch` must survive exactly as
 * given, because it is the only thing distinguishing an EHR launch from a
 * standalone one and therefore whether the launch names a patient. And the claim
 * code must travel in the POST body, since carrying it in a URL would give back
 * precisely what the claim indirection exists to avoid.
 */

const BASE = 'https://fhir-integration.test';
const ISS = 'https://ehr.example.com/fhir';
const CLAIM = 'Zm9vYmFy';

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function envelope(data: unknown): unknown {
  return { data, error: null };
}

function apiWith(fetchImpl: FetchLike) {
  return createLaunchApi(BASE, fetchImpl);
}

describe('authorizeUrl', () => {
  it('declares delivery=mobile', () => {
    const url = apiWith(async () => jsonResponse(200, envelope({}))).authorizeUrl({ iss: ISS });

    // Without this the callback renders JSON in the browser the EHR redirected
    // and the app that started the launch never learns its launch_id.
    expect(new URLSearchParams(url.split('?')[1]).get('delivery')).toBe(MOBILE_DELIVERY);
  });

  it('sends the EHR launch context when there is one', () => {
    const url = apiWith(async () => jsonResponse(200, envelope({}))).authorizeUrl({
      iss: ISS,
      launch: 'abc-123',
    });

    const parameters = new URLSearchParams(url.split('?')[1]);
    expect(parameters.get('iss')).toBe(ISS);
    expect(parameters.get('launch')).toBe('abc-123');
  });

  /**
   * A standalone launch sends no `launch` at all, and that absence is load
   * bearing. An empty `launch=` would present as an EHR launch carrying an empty
   * context, which the EHR answers by naming no patient — the same visible
   * outcome as a standalone launch, reached by telling the EHR something untrue.
   */
  it('omits the launch parameter entirely for a standalone launch', () => {
    const url = apiWith(async () => jsonResponse(200, envelope({}))).authorizeUrl({ iss: ISS });

    expect(url).not.toContain('launch=');
    expect(new URLSearchParams(url.split('?')[1]).has('launch')).toBe(false);
  });
});

describe('redeemClaim', () => {
  it('posts the code in the body and never in the URL', async () => {
    const fetchImpl = jest.fn<Promise<Response>, Parameters<FetchLike>>(async () =>
      jsonResponse(200, envelope({ launch_id: 'launch-7', ehr_type: 'athena', expires_in: 3600 })),
    );

    await apiWith(fetchImpl).redeemClaim(CLAIM);

    const [url, init] = fetchImpl.mock.calls[0]!;
    expect(url).toBe(`${BASE}/fhir/launch/claim`);
    expect(url).not.toContain(CLAIM);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ claim: CLAIM });
  });

  it('reads the launch back', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(200, envelope({ launch_id: 'launch-7', ehr_type: 'athena', expires_in: 3600 }));

    const result = await apiWith(fetchImpl).redeemClaim(CLAIM);

    expect(result).toEqual({
      ok: true,
      value: { launchId: 'launch-7', ehrType: 'athena', expiresInSeconds: 3600 },
    });
  });

  /**
   * The service answers one 404 for unknown, expired and already-redeemed, so
   * that a caller probing codes learns nothing about which were real. The client
   * passes that through rather than guessing at which of the three happened.
   */
  it('reports a spent or unknown code as the service described it', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(404, {
        data: null,
        error: { code: 'SMART_UNKNOWN_CLAIM', message: 'No such launch claim.' },
      });

    const result = await apiWith(fetchImpl).redeemClaim(CLAIM);

    expect(result).toEqual({
      ok: false,
      failure: {
        kind: 'status',
        status: 404,
        code: 'SMART_UNKNOWN_CLAIM',
        message: 'No such launch claim.',
      },
    });
  });

  it('reports a body with no launch_id as malformed rather than as a launch', async () => {
    const fetchImpl: FetchLike = async () => jsonResponse(200, envelope({ ehr_type: 'athena' }));

    const result = await apiWith(fetchImpl).redeemClaim(CLAIM);

    // A launch with no id is not a launch. Accepting one would put an empty
    // string in the header of every route that names a patient.
    expect(result.ok).toBe(false);
  });

  it('keeps a launch whose expiry did not survive the wire', async () => {
    const fetchImpl: FetchLike = async () =>
      jsonResponse(200, envelope({ launch_id: 'launch-7', ehr_type: 'athena' }));

    const result = await apiWith(fetchImpl).redeemClaim(CLAIM);

    // The number is reported, not acted on: the service renews the EHR token on
    // its own, so a missing expiry is not a reason to discard a usable launch.
    expect(result).toEqual({
      ok: true,
      value: { launchId: 'launch-7', ehrType: 'athena', expiresInSeconds: 0 },
    });
  });

  it('does not surface the thrown value when the request fails', async () => {
    const fetchImpl: FetchLike = async () => {
      throw new Error(`connect ECONNREFUSED ${BASE}/fhir/launch/claim?claim=${CLAIM}`);
    };

    const result = await apiWith(fetchImpl).redeemClaim(CLAIM);

    expect(result.ok).toBe(false);
    if (!result.ok) {
      // A thrown network error can name the request; this one's body is a live
      // credential.
      expect(result.failure.kind).toBe('network');
      expect(result.failure.message).not.toContain(CLAIM);
    }
  });
});
