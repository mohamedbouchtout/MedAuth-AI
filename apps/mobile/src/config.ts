/**
 * Runtime configuration.
 *
 * Expo inlines `EXPO_PUBLIC_*` variables at build time, so anything here is
 * readable in the shipped bundle. That is fine for a host name and fatal for a
 * credential — the session JWT is minted per encounter by TASK-006 and passed
 * into the hook, never configured here.
 */

/**
 * The audio-ingestion WebSocket origin — origin only, no path; the hook appends
 * `/ws/audio/{session_id}`.
 *
 * `EXPO_PUBLIC_AUDIO_WS_URL` already existed in `.env.example` from the TASK-001
 * scaffold; this is that variable, not a new one. The local-dev default matches
 * the port table in CLAUDE.md. Any deployed build must set a `wss://` origin —
 * this socket carries encounter audio and a session credential, and CLAUDE.md
 * requires TLS everywhere. `ws://` is a localhost-only convenience.
 */
export const AUDIO_INGESTION_WS_URL =
  process.env.EXPO_PUBLIC_AUDIO_WS_URL ?? 'ws://localhost:8001';

/**
 * The HTTP origin for the session lifecycle endpoints — origin only, no path;
 * the client appends `/sessions/...`.
 *
 * This is `EXPO_PUBLIC_API_BASE_URL`, which has been in `.env.example` since the
 * TASK-001 scaffold and was unused until TASK-025 — it is not a new variable.
 * It is deliberately *not* `AUDIO_INGESTION_WS_URL`: that one is a WebSocket
 * origin consumed by `useAudioCapture`, and putting a `ws://` scheme in front of
 * a REST path would fail in a way that looks like a routing bug rather than a
 * configuration one. The local-dev default is track-a-clinical's port from the
 * table in CLAUDE.md, because that service owns session lifecycle.
 *
 * A deployed build must set an `https://` origin: the start-visit body carries a
 * patient identifier and the responses carry a session credential.
 */
export const API_BASE_URL = process.env.EXPO_PUBLIC_API_BASE_URL ?? 'http://localhost:8003';

/**
 * The nudge-service WebSocket origin — origin only, no path; the hook appends
 * `/ws/nudges/{session_id}`.
 *
 * `EXPO_PUBLIC_NUDGE_WS_URL` has been in `.env.example` since the TASK-001
 * scaffold and was never exported from this module until TASK-043 — a variable
 * present in that file is evidence of intent, not of wiring, and the same gap
 * existed on the web side until TASK-042. The local-dev default is
 * nudge-service's port from the table in CLAUDE.md.
 *
 * A deployed build must set a `wss://` origin: this socket carries a session
 * credential in its handshake and a live stream of PHI afterwards — a nudge
 * names a procedure and the payer criteria an encounter has not documented.
 */
export const NUDGE_SERVICE_WS_URL =
  process.env.EXPO_PUBLIC_NUDGE_WS_URL ?? 'ws://localhost:8005';

/**
 * The HTTP origin for track-b-rag — origin only, no path; the client appends
 * `/nudges/...`.
 *
 * **This is the second HTTP origin this app needs, and it is not `API_BASE_URL`.**
 * That one is track-a-clinical, which owns the session lifecycle and mints the
 * tokens these sockets need; the nudge acknowledge route (TASK-041b) belongs to
 * track-b-rag, a different service on a different port. `apps/web` hit this
 * first in TASK-042 and `.env.example` said in writing that mobile would not,
 * which stopped being true the moment this app grew a dismiss button.
 *
 * Do not collapse the two into one base URL until something actually puts both
 * services behind one origin — the Phase 6 gateway CLAUDE.md defers under "CORS
 * and browser reachability". A deployed build must set an `https://` origin.
 */
export const TRACK_B_RAG_URL =
  process.env.EXPO_PUBLIC_TRACK_B_RAG_URL ?? 'http://localhost:8002';

/**
 * The HTTP origin for fhir-integration — origin only, no path; the client
 * appends `/fhir/...`.
 *
 * **This is the third HTTP origin this app needs, and it is not either of the
 * others.** `API_BASE_URL` is track-a-clinical, which owns the session
 * lifecycle; `TRACK_B_RAG_URL` is track-b-rag, which owns nudge acknowledgement;
 * this is fhir-integration, which owns the two routes that say which patient a
 * visit is about (TASK-025b). Three services, three ports, three variables —
 * collapsing them waits on the Phase 6 gateway CLAUDE.md defers to under "CORS
 * and browser reachability".
 *
 * `EXPO_PUBLIC_FHIR_BASE_URL` is added to `.env.example` by TASK-025b; unlike
 * the four before it, it is genuinely new rather than one that had been sitting
 * there unread. The local-dev default is fhir-integration's port from the table
 * in CLAUDE.md.
 *
 * A deployed build must set an `https://` origin: the search sends a patient's
 * name in a query string, its answer carries patient identifiers, and every call
 * carries a `launch_id` that resolves to an EHR access token.
 */
export const FHIR_INTEGRATION_URL =
  process.env.EXPO_PUBLIC_FHIR_BASE_URL ?? 'http://localhost:8004';

/**
 * True when the configured origin is not TLS-protected.
 *
 * Covers both schemes this app configures — `ws://` for the two WebSocket
 * origins and `http://` for the three HTTP ones — because the rule they are
 * checked against is one rule, CLAUDE.md's "TLS everywhere", and a second
 * near-identical helper is how one origin ends up quietly exempt.
 */
export function isInsecureOrigin(origin: string): boolean {
  return origin.startsWith('ws://') || origin.startsWith('http://');
}
