/**
 * Client for fhir-integration's note write-back (TASK-053), used by TASK-071.
 *
 * **The body is `{session_id}` and nothing else.** The review screen is holding
 * the note text and its codes at the moment this is called, and posting them
 * back would be shorter here and wrong: `fhir-integration` reads them from
 * `track-a-clinical` over HTTP so that the owning service writes the `READ_NOTE`
 * row every other read of a note's content writes. A client-supplied body
 * produces no such row, because from that service's point of view nothing was
 * read. CLAUDE.md settles it under "Writing clinical data out to the EHR".
 *
 * **The launch goes in a header, never the body and never the URL.** A
 * `launch_id` resolves to an EHR access token, so it is a capability handle by
 * this repository's own definition and the one place it must not appear is a
 * query string. `LAUNCH_ID_HEADER` comes from `@medauth/fhir-client` rather than
 * being spelled again here — one definition of the header name, shared with the
 * three routes that already send it.
 *
 * **Why this is not in `@medauth/fhir-client` despite being that service's
 * route.** That package exists for what two apps share, and `apps/mobile` has no
 * note review screen to file a note from. Its scope note already anticipates
 * this route's home; the trigger to move it is a second consumer, exactly as it
 * was for the three packages that exist. Until then a shared module with one
 * caller is a guess at an interface.
 *
 * Nothing here logs: the URL names a session and the header carries a
 * credential.
 */

import { LAUNCH_ID_HEADER } from '@medauth/fhir-client';
import type { ApiFailure, ApiResult } from '@medauth/session-client';

import { FHIR_INTEGRATION_URL } from '../config';

/** The error code a note already on the chart answers with. */
export const ALREADY_WRITTEN_CODE = 'NOTE_ALREADY_WRITTEN_TO_EHR';

/** The error code for a visit that was never linked to a chart entry. */
export const NOT_LINKED_CODE = 'ENCOUNTER_NOT_LINKED_TO_EHR';

/**
 * The one failure where the chart *did* change.
 *
 * The document was created and recording its id here failed, so the operation
 * must never be retried: a second attempt files a second copy of one encounter's
 * note on a patient's chart. The message names the created document.
 */
export const RECORD_FAILED_CODE = 'EHR_NOTE_RECORD_FAILED';

/** What a successful write-back reports. */
export interface WrittenNote {
  sessionId: string;
  /** The id of the DocumentReference the EHR created. */
  ehrDocumentRefId: string;
}

export interface EhrNotesApi {
  /** Files a session's note to the chart. `launchId` holds the EHR credential. */
  writeNote(sessionId: string, launchId: string): Promise<ApiResult<WrittenNote>>;
}

export type FetchLike = (url: string, init: RequestInit) => Promise<Response>;

const MALFORMED: ApiFailure = {
  kind: 'malformed',
  message: 'The server returned a response MedAuth AI could not read.',
};

function networkFailure(): ApiFailure {
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

function readWritten(body: unknown): WrittenNote | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const { session_id: sessionId, ehr_document_ref_id: documentId } = data as Record<
    string,
    unknown
  >;
  if (typeof sessionId !== 'string' || typeof documentId !== 'string') {
    return null;
  }
  return { sessionId, ehrDocumentRefId: documentId };
}

export function createEhrNotesApi(
  baseUrl: string = FHIR_INTEGRATION_URL,
  fetchImpl: FetchLike = (url, init) => fetch(url, init),
): EhrNotesApi {
  return {
    async writeNote(sessionId, launchId) {
      let response: Response;
      try {
        response = await fetchImpl(`${baseUrl}/fhir/notes`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            [LAUNCH_ID_HEADER]: launchId,
          },
          body: JSON.stringify({ session_id: sessionId }),
        });
      } catch {
        return { ok: false, failure: networkFailure() };
      }

      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        if (response.ok) {
          return { ok: false, failure: MALFORMED };
        }
      }

      if (!response.ok) {
        return { ok: false, failure: readError(body, response.status) };
      }

      const written = readWritten(body);
      return written === null ? { ok: false, failure: MALFORMED } : { ok: true, value: written };
    },
  };
}

/** The client the review screen uses when none is injected. */
export const ehrNotesApi = createEhrNotesApi();
