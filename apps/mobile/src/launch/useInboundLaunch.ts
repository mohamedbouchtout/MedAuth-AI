/**
 * The EHR-initiated launch this app was opened with, if it was (TASK-025c).
 *
 * Two sources, because an app can be reached in two states and only one of them
 * fires an event: `getInitialURL` covers a cold start — the EHR opened MedAuth
 * and the process did not exist — and the `url` listener covers a launch
 * arriving while the app is already running. Handling only the second is the
 * common mistake, and it fails in exactly the case a provider is most likely to
 * hit first.
 *
 * React Native's own `Linking` rather than `expo-linking`: this needs the two
 * primitives above and nothing else, and they are in the framework already.
 */

import { useEffect, useState } from 'react';
import { Linking } from 'react-native';

import type { LaunchRequest } from '../api/launch';

import { launchRequestFromUrl } from './inbound';

/**
 * Returns the inbound launch request, or null when the app was opened directly.
 *
 * A URL that is not a launch leaves the state alone rather than clearing it: an
 * OS may hand over any link at all, and a later unrelated one must not discard a
 * launch the provider is part-way through.
 */
export function useInboundLaunch(): LaunchRequest | null {
  const [request, setRequest] = useState<LaunchRequest | null>(null);

  useEffect(() => {
    let cancelled = false;

    function accept(url: string | null): void {
      if (url === null) {
        return;
      }
      const parsed = launchRequestFromUrl(url);
      if (parsed !== null && !cancelled) {
        setRequest(parsed);
      }
    }

    void Linking.getInitialURL().then(accept, () => {
      // A platform that cannot answer leaves this app in the standalone case,
      // which is a working flow rather than an error. Not logged: the URL this
      // failed to read carries the EHR's launch context.
    });
    const subscription = Linking.addEventListener('url', ({ url }) => accept(url));

    return () => {
      cancelled = true;
      subscription.remove();
    };
  }, []);

  return request;
}
