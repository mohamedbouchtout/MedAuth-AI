/**
 * This app's binding of the shared session client.
 *
 * The client itself is `@medauth/session-client` — it moved out of this
 * directory in TASK-042, when `apps/web` became the second consumer of the
 * re-mint path. What stays here is the one thing that is genuinely this app's:
 * which host to talk to. `EXPO_PUBLIC_API_BASE_URL` is not interchangeable with
 * `EXPO_PUBLIC_AUDIO_WS_URL`, which is a WebSocket origin.
 *
 * Types are re-exported so callers in this app keep importing from one place;
 * the package is the definition, this is the local name for it.
 */

import { createSessionsApi } from '@medauth/session-client';

import { API_BASE_URL } from '../config';

export type {
  ApiFailure,
  ApiResult,
  Session,
  SessionsApi,
  StartVisitInput,
} from '@medauth/session-client';

/** The client the screen uses when none is injected. */
export const sessionsApi = createSessionsApi(API_BASE_URL);
