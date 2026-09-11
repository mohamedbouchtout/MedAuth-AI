import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { LaunchApi } from '../../../src/api/fhirClient';
import { LaunchScreen, NO_ISSUER_MESSAGE } from '../../../src/screens/LaunchScreen';

/**
 * Starting a standalone SMART launch from the browser (TASK-070).
 *
 * An EHR-initiated launch never reaches this screen — the EHR opens
 * `GET /fhir/launch` itself and the provider arrives holding a claim code. What
 * is asserted here is the standalone case, and above all the refusal: with no
 * issuer configured there is no launch to offer, and a button that cannot work
 * would fail at SMART discovery in a way that reads as the EHR being down.
 */

const ISS = 'https://ehr.example.com/fhir';

function launches(): LaunchApi {
  return {
    authorizeUrl: vi.fn(({ iss }) => `https://fhir.test/fhir/launch?iss=${iss}&delivery=web`),
    redeemClaim: vi.fn(),
  };
}

describe('with an issuer configured', () => {
  it('navigates to the authorize URL', () => {
    const navigate = vi.fn();
    const api = launches();
    render(<LaunchScreen iss={ISS} launches={api} navigate={navigate} />);

    fireEvent.click(screen.getByTestId('begin-launch'));

    expect(api.authorizeUrl).toHaveBeenCalledWith({ iss: ISS });
    expect(navigate).toHaveBeenCalledWith(`https://fhir.test/fhir/launch?iss=${ISS}&delivery=web`);
  });

  it('shows a failure from a launch that did not complete', () => {
    render(
      <LaunchScreen
        iss={ISS}
        failure="MedAuth AI could not complete the EHR sign-in."
        launches={launches()}
        navigate={vi.fn()}
      />,
    );

    expect(screen.getByTestId('launch-failed')).toHaveTextContent('could not complete');
    // Still offered: the provider's next move is to try again.
    expect(screen.getByTestId('begin-launch')).toBeInTheDocument();
  });
});

describe('with no issuer configured', () => {
  it('offers no launch at all and says why', () => {
    render(<LaunchScreen iss="" launches={launches()} navigate={vi.fn()} />);

    expect(screen.getByTestId('no-issuer')).toHaveTextContent(NO_ISSUER_MESSAGE);
    expect(screen.queryByTestId('begin-launch')).not.toBeInTheDocument();
  });
});
