import { queryParam } from '../../../src/launch/uri';

/**
 * The custom-scheme query parser (TASK-025c).
 *
 * It exists because React Native's `URL` handles a hostless custom-scheme URI
 * unpredictably, and the failure that produces is silent: a launch that
 * completed and delivered nothing. So the cases here are the shapes that
 * actually arrive from an OS redirect, not a general URL suite.
 */

describe('queryParam', () => {
  it('reads a value from a custom-scheme redirect', () => {
    expect(queryParam('medauth://launch?claim=abc123', 'claim')).toBe('abc123');
  });

  it('reads a value that is not the first parameter', () => {
    expect(queryParam('medauth://launch?iss=https%3A%2F%2Fe.test&launch=ctx', 'launch')).toBe('ctx');
  });

  it('decodes a percent-encoded value', () => {
    // An `iss` is a URL, so it arrives encoded whenever it is a query value.
    expect(queryParam('medauth://launch?iss=https%3A%2F%2Fehr.test%2Ffhir', 'iss')).toBe(
      'https://ehr.test/fhir',
    );
  });

  it('does not match a parameter whose name merely ends with the one asked for', () => {
    // `launch` and `relaunch` are one substring apart, and a launch context read
    // off the wrong parameter would be sent to the EHR as fact.
    expect(queryParam('medauth://launch?relaunch=no', 'launch')).toBeNull();
  });

  it('skips a bare parameter carrying no value', () => {
    // An OS or a proxy can append a valueless flag; skipping it rather than
    // mis-splitting is what keeps the parameter after it readable.
    expect(queryParam('medauth://launch?flag&claim=abc', 'claim')).toBe('abc');
  });

  it('returns null for a URI with no query at all', () => {
    expect(queryParam('medauth://launch', 'claim')).toBeNull();
  });

  it('returns null for an absent parameter', () => {
    expect(queryParam('medauth://launch?other=1', 'claim')).toBeNull();
  });

  /**
   * Empty and absent are one answer, because no caller can act on the
   * difference: a claim code that is the empty string buys no launch, and an
   * `iss` that is the empty string names no EHR.
   */
  it('treats an empty value as absent', () => {
    expect(queryParam('medauth://launch?claim=', 'claim')).toBeNull();
  });

  it('ignores a fragment after the query', () => {
    expect(queryParam('medauth://launch?claim=abc#state', 'claim')).toBe('abc');
  });
});
