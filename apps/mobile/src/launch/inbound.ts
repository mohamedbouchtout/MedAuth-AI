/**
 * An EHR-initiated launch arriving as a deep link (TASK-025c).
 *
 * **The two launch types differ before this app does anything.** A standalone
 * launch is a provider opening MedAuth directly, so the app supplies the issuer
 * from configuration and the EHR names no patient. An EHR launch is the EHR
 * opening MedAuth from a chart, which on this platform means a link into the
 * app carrying `iss` and the opaque `launch` context — and that context is the
 * only reason the completed launch knows which patient is in the room.
 *
 * So the presence of `launch` is never assumed, defaulted or invented: it is
 * what decides whether `GET /fhir/launch-context` answers with a patient or
 * with nulls, and therefore whether TASK-025b's screen resolves straight
 * through or falls to a search.
 *
 * **An inbound URL is untrusted input.** `iss` picked up here goes on to
 * fhir-integration, which resolves the vendor from it and reads that EHR's
 * SMART discovery document — so a hostile link naming a hostile issuer would
 * send a provider to a hostile login page. What contains that is not this
 * parser: it is that no credential of ours is spent before the provider
 * authenticates, and that a launch completing against an unknown issuer yields
 * a launch whose EHR holds no patients this practice recognises. It is recorded
 * here because it is the sort of thing a later reader should not have to infer,
 * and because binding launches to an allow-list of configured issuers is the
 * natural hardening once more than one issuer exists.
 */

import type { LaunchRequest } from '@medauth/fhir-client';

import { queryParam } from './uri';

/**
 * Turn an inbound deep link into a launch request, or null when it is not one.
 *
 * Null is the common case rather than an error: this app's scheme also carries
 * the auth session's own redirect, which is consumed by the browser session and
 * never by a link handler, and an OS may hand over any URL at all. A link with
 * no `iss` names no EHR and cannot start a launch.
 */
export function launchRequestFromUrl(url: string): LaunchRequest | null {
  const iss = queryParam(url, 'iss');
  if (iss === null) {
    return null;
  }
  const launch = queryParam(url, 'launch');
  // Spread rather than always setting the key: `exactOptionalPropertyTypes` is
  // on, and an explicit `undefined` is not the same as an absent `launch` — the
  // authorize URL branches on exactly that.
  return launch === null ? { iss } : { iss, launch };
}
