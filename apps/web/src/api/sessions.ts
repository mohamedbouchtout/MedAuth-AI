/**
 * This app's binding of the shared session client.
 *
 * The client itself is `@medauth/session-client`, which `apps/mobile` and this
 * app both import — see that package's note for why one definition of
 * "re-mint, never re-start" is worth more than two tidy local copies. What stays
 * here is the one thing that is genuinely this app's: which host to talk to.
 *
 * `API_BASE_URL` is track-a-clinical. It is not `TRACK_B_RAG_URL`, which is a
 * different service, and not either WebSocket origin.
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

/** The client the nudge hook uses when none is injected. */
export const sessionsApi = createSessionsApi(API_BASE_URL);
