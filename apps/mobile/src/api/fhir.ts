/**
 * Client for the two fhir-integration routes that identify a patient (TASK-025b).
 *
 * `GET /fhir/launch-context` (TASK-051d) says who the EHR launched us for, and
 * `GET /fhir/patient/search` (TASK-025b) finds a patient when nobody did. They
 * answer two different launch types and neither replaces the other — see
 * `patientSource` for the order they are used in.
 *
 * **Both are keyed on `launch_id`, carried in the `X-MedAuth-Launch-Id`
 * header.** Never a query parameter: a `launch_id` resolves to an EHR access
 * token, which makes it a credential, and this repository keeps that class of
 * value out of URLs that intermediaries log. It is also not a `session_id` — a
 * launch precedes the visit and outlives several of them, per CLAUDE.md's
 * "A SMART launch is not an encounter session".
 *
 * Failures are returned as typed results and never thrown, per CLAUDE.md's
 * TypeScript conventions. Nothing here logs: a search query is a patient's name
 * and every successful response carries patient identifiers.
 *
 * **This lives in the app rather than in `packages/`, for now.** `apps/web` will
 * need the same two calls in TASK-070, and that is the point at which it moves —
 * the same trigger that extracted `session-client` from this app in TASK-042 and
 * `nudge-client` from the web app in TASK-043. Extracting it before a second
 * consumer exists would be guessing at the shape the second one needs.
 */

import type { ApiFailure, ApiResult, FetchLike } from '@medauth/session-client';

/** The header both routes take the launch in. Spelled once. */
export const LAUNCH_ID_HEADER = 'X-MedAuth-Launch-Id';

/** What the EHR named when it launched us. Any field may be absent. */
export interface LaunchContext {
  /** The patient the EHR put in scope, or null for a standalone launch. */
  patientId: string | null;
  /** The chart entry the launch came from, or null when it came from none. */
  encounterId: string | null;
  /** The provider, already resolved to a UUID by the service. */
  providerId: string | null;
}

/** One candidate from a name search. */
export interface PatientMatch {
  patientId: string;
  familyName: string | null;
  givenNames: string[];
  birthDate: string | null;
  gender: string | null;
}

export interface PatientSearchResults {
  matches: PatientMatch[];
  /**
   * Whether the EHR held more matches than were returned.
   *
   * Rendered rather than ignored: a provider shown twenty of two hundred Smiths
   * and not told so concludes the patient they want is not in the system.
   */
  truncated: boolean;
}

export interface FhirApi {
  getLaunchContext(launchId: string): Promise<ApiResult<LaunchContext>>;
  searchPatients(
    launchId: string,
    query: string,
    birthDate?: string,
  ): Promise<ApiResult<PatientSearchResults>>;
}

const MALFORMED: ApiFailure = {
  kind: 'malformed',
  message: 'The server returned a response MedAuth AI could not read.',
};

function networkFailure(): ApiFailure {
  // The thrown value is not surfaced: it can carry the request URL, and this
  // one's query string carries a patient's name.
  return {
    kind: 'network',
    message: 'MedAuth AI could not reach the server. Check the network connection.',
  };
}

function readError(body: unknown, status: number): ApiFailure {
  const error = (body as { error?: unknown } | null)?.error;
  if (typeof error === 'object' && error !== null) {
    const { code, message } = error as { code?: unknown; message?: unknown };
    if (typeof code === 'string' && typeof message === 'string') {
      return { kind: 'status', status, code, message };
    }
  }
  return { kind: 'status', status, code: 'unknown', message: `The server returned ${status}.` };
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function readLaunchContext(body: unknown): LaunchContext | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const record = data as Record<string, unknown>;
  return {
    patientId: optionalString(record.patient_id),
    encounterId: optionalString(record.encounter_id),
    providerId: optionalString(record.provider_id),
  };
}

function readMatch(value: unknown): PatientMatch | null {
  if (typeof value !== 'object' || value === null) {
    return null;
  }
  const record = value as Record<string, unknown>;
  // The id is the only field a selection cannot work without; the rest are
  // display detail the EHR may legitimately not hold.
  if (typeof record.patient_id !== 'string') {
    return null;
  }
  const given = record.given_names;
  return {
    patientId: record.patient_id,
    familyName: optionalString(record.family_name),
    givenNames: Array.isArray(given) ? given.filter((name): name is string => typeof name === 'string') : [],
    birthDate: optionalString(record.birth_date),
    gender: optionalString(record.gender),
  };
}

function readSearchResults(body: unknown): PatientSearchResults | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const record = data as Record<string, unknown>;
  if (!Array.isArray(record.matches)) {
    return null;
  }
  const matches: PatientMatch[] = [];
  for (const entry of record.matches) {
    const match = readMatch(entry);
    // A candidate this app cannot render is dropped rather than failing the
    // whole search: the other matches are still usable, and the one the
    // provider wants may well be among them.
    if (match !== null) {
      matches.push(match);
    }
  }
  return { matches, truncated: record.truncated === true };
}

export function createFhirApi(
  baseUrl: string,
  fetchImpl: FetchLike = (url, init) => fetch(url, init),
): FhirApi {
  async function get(path: string, launchId: string): Promise<ApiResult<unknown>> {
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method: 'GET',
        headers: { Accept: 'application/json', [LAUNCH_ID_HEADER]: launchId },
      });
    } catch {
      return { ok: false, failure: networkFailure() };
    }

    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // Only fatal on the success path — an error status still tells the caller
      // what happened without a parseable body.
      if (response.ok) {
        return { ok: false, failure: MALFORMED };
      }
    }

    if (!response.ok) {
      return { ok: false, failure: readError(body, response.status) };
    }
    return { ok: true, value: body };
  }

  return {
    async getLaunchContext(launchId) {
      const result = await get('/fhir/launch-context', launchId);
      if (!result.ok) {
        return result;
      }
      const context = readLaunchContext(result.value);
      return context === null ? { ok: false, failure: MALFORMED } : { ok: true, value: context };
    },

    async searchPatients(launchId, query, birthDate) {
      const parameters = new URLSearchParams({ query });
      if (birthDate !== undefined && birthDate !== '') {
        parameters.set('birth_date', birthDate);
      }
      const result = await get(`/fhir/patient/search?${parameters.toString()}`, launchId);
      if (!result.ok) {
        return result;
      }
      const results = readSearchResults(result.value);
      return results === null ? { ok: false, failure: MALFORMED } : { ok: true, value: results };
    },
  };
}
