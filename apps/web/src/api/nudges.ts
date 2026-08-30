/**
 * This app's binding of the shared nudge client.
 *
 * The client itself is `@medauth/nudge-client`, which `apps/mobile` and this app
 * both import — see that package's note for why the acknowledge body and the
 * deliberately absent credential are defined once rather than twice. What stays
 * here is the one thing that is genuinely this app's: which host to talk to.
 *
 * `TRACK_B_RAG_URL` is track-b-rag, which owns the acknowledge route. It is not
 * `API_BASE_URL`, which is track-a-clinical and owns the session lifecycle;
 * pointing one at the other is the mistake the two variables exist to make
 * impossible, and CLAUDE.md defers collapsing them to the Phase 6 gateway.
 */

import { createNudgesApi } from '@medauth/nudge-client';

import { TRACK_B_RAG_URL } from '../config';

export type { Acknowledgement, NudgesApi } from '@medauth/nudge-client';

/** The client the overlay uses when none is injected. */
export const nudgesApi = createNudgesApi(TRACK_B_RAG_URL);
