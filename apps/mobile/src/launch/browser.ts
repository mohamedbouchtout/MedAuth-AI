/**
 * The system-browser binding for the launch flow.
 *
 * Kept apart from `./smartLaunch` — which holds the whole of the exchange's
 * logic — so that module can be driven in a test without the native module, and
 * so this file has nothing in it but the binding.
 *
 * `openAuthSessionAsync` rather than `openBrowserAsync`: it is the call that
 * returns the redirect to the app that opened the session, which is what keeps
 * the claim code's window narrow. And a system browser rather than a `WebView`,
 * because the provider types an EHR password into it.
 */

import * as WebBrowser from 'expo-web-browser';

import type { AuthSessionOpener } from './smartLaunch';

/** Opens the EHR's authorization page and waits for the redirect back. */
export const openAuthSession: AuthSessionOpener = (url, returnUri) =>
  WebBrowser.openAuthSessionAsync(url, returnUri);
