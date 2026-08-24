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

/** True when the configured origin is not TLS-protected. */
export function isInsecureOrigin(origin: string): boolean {
  return origin.startsWith('ws://');
}
