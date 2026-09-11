/**
 * Reading fhir-integration's response envelope, once.
 *
 * `./fhir` and `./launch` arrived here as two modules from `apps/mobile`, each
 * carrying its own identical `MALFORMED`, `networkFailure` and `readError`.
 * Keeping both copies inside one package would have preserved, in a smaller
 * space, exactly the divergence extracting the package was meant to end — so
 * they are one definition here.
 *
 * The envelope itself is CLAUDE.md's, fixed in the API Design section and
 * implemented service-side by `packages/api-envelope`: `{"data": ..., "error":
 * null}` or `{"data": null, "error": {...}}`. Nothing in this module knows what
 * is inside `data`; each caller narrows that for itself.
 *
 * **Nothing here surfaces a thrown value or a URL.** A fetch rejection can carry
 * the request URL, and the URLs this package builds carry a patient's name in a
 * search query string and a live claim code in a request body. The messages
 * below are fixed text for that reason, not for tidiness.
 */

import type { ApiFailure, ApiResult } from '@medauth/session-client';

/** The response parsed, but did not hold what the route promised. */
export const MALFORMED: ApiFailure = {
  kind: 'malformed',
  message: 'The server returned a response MedAuth AI could not read.',
};

/** The request never produced a response. */
export function networkFailure(): ApiFailure {
  return {
    kind: 'network',
    message: 'MedAuth AI could not reach the server. Check the network connection.',
  };
}

/**
 * Read the `error` half of the envelope, falling back to the bare status.
 *
 * The fallback matters: a 502 from a proxy in front of the service answers HTML
 * or nothing at all, and a caller still has to be told what happened.
 */
export function readError(body: unknown, status: number): ApiFailure {
  const error = (body as { error?: unknown } | null)?.error;
  if (typeof error === 'object' && error !== null) {
    const { code, message } = error as { code?: unknown; message?: unknown };
    if (typeof code === 'string' && typeof message === 'string') {
      return { kind: 'status', status, code, message };
    }
  }
  return { kind: 'status', status, code: 'unknown', message: `The server returned ${status}.` };
}

/**
 * Turn a `Response` into a result carrying its parsed body.
 *
 * A body that will not parse is fatal only on the success path — an error status
 * still tells the caller what happened without a parseable body, which is what a
 * proxy-generated 502 looks like.
 */
export async function readEnvelope(response: Response): Promise<ApiResult<unknown>> {
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
  return { ok: true, value: body };
}

/** Narrow an unknown to a string, or null. Used for every optional wire field. */
export function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}
