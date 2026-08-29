/// <reference types="vite/client" />

/**
 * Vite inlines `VITE_*` variables at build time, so anything declared here is
 * readable in the shipped bundle. That is fine for a host name and fatal for a
 * credential — the session JWT is minted per encounter by TASK-006 and passed
 * into the hooks, never configured here.
 */
interface ImportMetaEnv {
  /** audio-ingestion WebSocket origin — origin only, no path. */
  readonly VITE_AUDIO_WS_URL?: string;
  /** nudge-service WebSocket origin — origin only, no path. */
  readonly VITE_NUDGE_WS_URL?: string;
  /** track-a-clinical HTTP origin — the session lifecycle service. */
  readonly VITE_API_BASE_URL?: string;
  /** track-b-rag HTTP origin — where the nudge acknowledge route lives. */
  readonly VITE_TRACK_B_RAG_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
