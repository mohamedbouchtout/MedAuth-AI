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
 * True when the configured origin is not TLS-protected.
 *
 * Covers both schemes this app configures, because the rule they are checked
 * against is one rule — CLAUDE.md's "TLS everywhere" — and a second near-identical
 * helper is how one of the two ends up quietly exempt.
 */
export function isInsecureOrigin(origin: string): boolean {
  return origin.startsWith('ws://') || origin.startsWith('http://');
}
