import * as WebBrowser from 'expo-web-browser';

import { openAuthSession } from '../../../src/launch/browser';

/**
 * The system-browser binding (TASK-025c).
 *
 * One assertion, and it is the one that cannot be made anywhere else: that the
 * launch goes through `openAuthSessionAsync` rather than `openBrowserAsync`.
 * That call is what returns the redirect to the app that opened the session,
 * which is what keeps the claim code's window narrow — and swapping it for the
 * plain browser open would compile, run, and simply never come back.
 */

it('opens an auth session, so the redirect returns to this app', async () => {
  const spy = jest
    .spyOn(WebBrowser, 'openAuthSessionAsync')
    .mockResolvedValue({ type: 'cancel' } as WebBrowser.WebBrowserAuthSessionResult);

  await openAuthSession('https://ehr.test/authorize', 'medauth://launch');

  expect(spy).toHaveBeenCalledWith('https://ehr.test/authorize', 'medauth://launch');
  spy.mockRestore();
});
