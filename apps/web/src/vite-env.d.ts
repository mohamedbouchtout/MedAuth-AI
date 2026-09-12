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
  /** fhir-integration HTTP origin — the SMART launch and patient identity routes. */
  readonly VITE_FHIR_BASE_URL?: string;
  /** prior-auth HTTP origin — where a denied request is resubmitted (TASK-072). */
  readonly VITE_PRIOR_AUTH_URL?: string;
  /**
   * The EHR a standalone launch targets — a public FHIR base URL, not a
   * credential. Absent means this deployment offers no standalone launch.
   */
  readonly VITE_SMART_ISS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
