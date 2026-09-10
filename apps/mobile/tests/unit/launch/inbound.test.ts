import { launchRequestFromUrl } from '../../../src/launch/inbound';

/**
 * Reading an EHR-initiated launch off a deep link (TASK-025c).
 *
 * The distinction being tested is the one the whole task turns on: a link
 * carrying `launch` is an EHR launch and yields a patient, and a link without it
 * is not. Getting that wrong is not a rendering bug — it decides whether the
 * patient screen resolves straight through or offers a search.
 */

const ISS = 'https://ehr.example.com/fhir';

describe('launchRequestFromUrl', () => {
  it('reads an EHR launch, keeping the opaque launch context', () => {
    const url = `medauth://launch?iss=${encodeURIComponent(ISS)}&launch=ctx-9`;

    expect(launchRequestFromUrl(url)).toEqual({ iss: ISS, launch: 'ctx-9' });
  });

  /**
   * A link naming an issuer and no launch context is a standalone launch the EHR
   * happened to start. It is honoured as such — with `launch` genuinely absent
   * rather than empty, because the authorize URL branches on exactly that.
   */
  it('reads an issuer-only link with no launch context at all', () => {
    const request = launchRequestFromUrl(`medauth://launch?iss=${encodeURIComponent(ISS)}`);

    expect(request).toEqual({ iss: ISS });
    expect(request === null ? true : 'launch' in request).toBe(false);
  });

  it('is not a launch when the link names no issuer', () => {
    // The auth session's own redirect has this shape. It is consumed by the
    // browser session and must never be mistaken for a new launch.
    expect(launchRequestFromUrl('medauth://launch?claim=abc123')).toBeNull();
  });

  it('is not a launch for an unrelated link', () => {
    expect(launchRequestFromUrl('medauth://something-else')).toBeNull();
  });
});
