/**
 * This app's binding of the shared nudge client.
 *
 * The client itself is `@medauth/nudge-client`, extracted from `apps/web` in
 * TASK-043 when this app became the second one dismissing a nudge. What stays
 * here is the one thing that is genuinely this app's: which host to talk to.
 *
 * `TRACK_B_RAG_URL` is track-b-rag, which owns the acknowledge route. It is not
 * `API_BASE_URL`, which is track-a-clinical and owns the session lifecycle —
 * this app needed only that one until the banner arrived, and `.env.example`
 * said so in writing until this task corrected it.
 */

import { createNudgesApi } from '@medauth/nudge-client';

import { TRACK_B_RAG_URL } from '../config';

export type { Acknowledgement, NudgesApi } from '@medauth/nudge-client';

/** The client the banner uses when none is injected. */
export const nudgesApi = createNudgesApi(TRACK_B_RAG_URL);
