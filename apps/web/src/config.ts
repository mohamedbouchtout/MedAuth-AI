/**
 * Runtime configuration.
 *
 * Vite inlines `VITE_*` variables at build time, so everything here ends up
 * readable in the shipped bundle. Host names belong here; the session JWT does
 * not — it is minted per encounter by TASK-006 and handed to the hooks.
 */

/**
 * The audio-ingestion WebSocket origin — origin only, no path; the hook appends
 * `/ws/audio/{session_id}`.
 *
 * `VITE_AUDIO_WS_URL` already existed in `.env.example` from the TASK-001
 * scaffold; this is that variable, not a new one. The local-dev default matches
 * the port table in CLAUDE.md. Any deployed build must set a `wss://` origin —
 * this socket carries encounter audio and a session credential, and CLAUDE.md
 * requires TLS everywhere. `ws://` is a localhost-only convenience.
 */
export const AUDIO_INGESTION_WS_URL = import.meta.env.VITE_AUDIO_WS_URL ?? 'ws://localhost:8001';

/**
 * The nudge-service WebSocket origin — origin only; `useNudgeStream` appends
 * `/ws/nudges/{session_id}` (TASK-042).
 *
 * Same TLS rule as the audio origin, and for a stronger reason than symmetry:
 * this socket carries the one live stream of PHI a browser sees in this
 * repository, and the handshake carries the session token.
 */
export const NUDGE_SERVICE_WS_URL = import.meta.env.VITE_NUDGE_WS_URL ?? 'ws://localhost:8005';

/**
 * The track-a-clinical HTTP origin — the session lifecycle service.
 *
 * This is what `POST /sessions/{session_id}/token` is called on when a held
 * token is near `exp`. It is deliberately not the same variable as the audio or
 * nudge origins: those are WebSocket schemes, and a build that reuses one for
 * the other fails in a way that reads like a routing bug.
 */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8003';

/**
 * The track-b-rag HTTP origin — where `PATCH /nudges/{nudge_id}/acknowledge`
 * lives (TASK-041b).
 *
 * A second HTTP origin rather than a path under `API_BASE_URL`, because these
 * are two services on two ports. `apps/mobile` needs only one and so has no
 * counterpart to this variable. Collapsing the two becomes correct when
 * something actually puts both services behind one origin — the gateway
 * CLAUDE.md defers to Phase 6 under "CORS and browser reachability" — and not
 * before.
 */
export const TRACK_B_RAG_URL = import.meta.env.VITE_TRACK_B_RAG_URL ?? 'http://localhost:8002';

/** True when the configured origin is not TLS-protected. */
export function isInsecureOrigin(origin: string): boolean {
  return origin.startsWith('ws://');
}
