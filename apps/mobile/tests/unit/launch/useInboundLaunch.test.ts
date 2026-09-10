import { renderHook, waitFor } from '@testing-library/react-native';
import { Linking } from 'react-native';

import { useInboundLaunch } from '../../../src/launch/useInboundLaunch';

/**
 * How an EHR-initiated launch reaches the app (TASK-025c).
 *
 * Two sources rather than one, and the cold-start half is the one a provider
 * hits first: the EHR opens MedAuth when the process does not exist, so no `url`
 * event ever fires and only `getInitialURL` answers. Handling only the event
 * listener is the common mistake and it fails in exactly that case.
 */

const ISS = 'https://ehr.example.com/fhir';
const LAUNCH_URL = `medauth://launch?iss=${encodeURIComponent(ISS)}&launch=ctx-9`;

type UrlListener = (event: { url: string }) => void;

function mockLinking(initialUrl: string | null): { emit: (url: string) => void; remove: jest.Mock } {
  const listeners: UrlListener[] = [];
  const remove = jest.fn();
  jest.spyOn(Linking, 'getInitialURL').mockResolvedValue(initialUrl);
  jest
    .spyOn(Linking, 'addEventListener')
    .mockImplementation((_event: string, listener: UrlListener) => {
      listeners.push(listener);
      return { remove } as unknown as ReturnType<typeof Linking.addEventListener>;
    });
  return {
    emit: (url: string) => listeners.forEach((listener) => listener({ url })),
    remove,
  };
}

afterEach(() => {
  jest.restoreAllMocks();
});

it('reads a launch the app was cold-started with', async () => {
  mockLinking(LAUNCH_URL);

  const { result } = await renderHook(() => useInboundLaunch());

  await waitFor(() => expect(result.current).toEqual({ iss: ISS, launch: 'ctx-9' }));
});

it('reads a launch arriving while the app is already running', async () => {
  const linking = mockLinking(null);

  const { result } = await renderHook(() => useInboundLaunch());
  await waitFor(() => expect(result.current).toBeNull());

  // Emitted outside `act`: this suite's environment does not configure one, and
  // wrapping the emit there swallows the update instead of flushing it. `waitFor`
  // is what settles the render.
  linking.emit(LAUNCH_URL);

  await waitFor(() => expect(result.current).toEqual({ iss: ISS, launch: 'ctx-9' }));
});

it('holds nothing when the app was opened directly', async () => {
  mockLinking(null);

  const { result } = await renderHook(() => useInboundLaunch());

  // The standalone case, and a working flow rather than an error: the app has a
  // configured issuer and waits for the provider to tap.
  await waitFor(() => expect(result.current).toBeNull());
});

/**
 * A later unrelated link must not discard a launch already in hand. An OS hands
 * over any URL registered to this scheme, including the auth session's own
 * redirect, and clearing on one of those would drop the launch mid-flow.
 */
it('leaves an existing launch alone when an unrelated link arrives', async () => {
  const linking = mockLinking(LAUNCH_URL);

  const { result } = await renderHook(() => useInboundLaunch());
  await waitFor(() => expect(result.current).not.toBeNull());

  linking.emit('medauth://launch?claim=abc123');

  await waitFor(() => expect(result.current).toEqual({ iss: ISS, launch: 'ctx-9' }));
});

it('stops listening when the app unmounts', async () => {
  const linking = mockLinking(null);

  const { unmount } = await renderHook(() => useInboundLaunch());
  await unmount();

  expect(linking.remove).toHaveBeenCalled();
});

it('leaves the app in the standalone case when the platform cannot answer', async () => {
  jest.spyOn(Linking, 'getInitialURL').mockRejectedValue(new Error('unsupported'));
  jest
    .spyOn(Linking, 'addEventListener')
    .mockReturnValue({ remove: jest.fn() } as unknown as ReturnType<typeof Linking.addEventListener>);

  const { result } = await renderHook(() => useInboundLaunch());

  // A rejected lookup is not a crash and not an error state: a standalone launch
  // is still available, and the URL that failed to read is not logged.
  await waitFor(() => expect(result.current).toBeNull());
});
