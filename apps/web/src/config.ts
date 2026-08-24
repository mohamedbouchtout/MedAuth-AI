/**
 * Runtime configuration.
 *
 * Vite inlines `VITE_*` variables at build time, so everything here ends up
 * readable in the shipped bundle. Host names belong here; the session JWT does
 * not — it is minted per encounter by TASK-006 and handed to the capture hook.
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

/** True when the configured origin is not TLS-protected. */
export function isInsecureOrigin(origin: string): boolean {
  return origin.startsWith('ws://');
}
