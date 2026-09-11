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

/**
 * The fhir-integration HTTP origin — origin only; each client appends its own
 * path under `/fhir/`.
 *
 * **This is the third HTTP origin this app needs, and it is not either of the
 * others.** `API_BASE_URL` is track-a-clinical, which owns the session
 * lifecycle; `TRACK_B_RAG_URL` is track-b-rag, which owns nudge
 * acknowledgement; this is fhir-integration, which owns obtaining a SMART
 * launch (TASK-051f) and the two routes keyed on one that say which patient a
 * visit is about (TASK-051d, TASK-025b). Three services, three ports, three
 * variables — collapsing them waits on the Phase 6 gateway CLAUDE.md defers to
 * under "CORS and browser reachability".
 *
 * `VITE_FHIR_BASE_URL` is added to `.env.example` by TASK-070 and is genuinely
 * new rather than one that had been sitting there unread. It is named to match
 * `apps/mobile`'s `EXPO_PUBLIC_FHIR_BASE_URL`, which points at the same service.
 * The local-dev default is fhir-integration's port from the table in CLAUDE.md.
 *
 * A deployed build must set an `https://` origin: the search sends a patient's
 * name in a query string, its answer carries patient identifiers, and every call
 * carries either a claim code or a `launch_id` that resolves to an EHR access
 * token.
 */
export const FHIR_INTEGRATION_URL =
  import.meta.env.VITE_FHIR_BASE_URL ?? 'http://localhost:8004';

/**
 * The EHR a standalone SMART launch targets — its FHIR base URL, the `iss` in
 * SMART's own vocabulary.
 *
 * **Only a standalone launch needs this.** An EHR-initiated launch arrives
 * carrying its own `iss`, because the EHR is the party naming itself; a provider
 * opening MedAuth directly has told us nothing, so the app supplies it.
 *
 * **Empty is a real state and the app says so.** With this unset there is no
 * standalone launch to offer, and the launch screen reports exactly that rather
 * than showing a button that cannot work. Launching against an empty issuer
 * would fail at SMART discovery instead, which reads as the EHR being down.
 *
 * **One configured issuer is a scope limit, not a design.** Unlike the origins
 * above, which genuinely are deployment-wide constants, an issuer is
 * per-practice as well as per-vendor. There is one pilot-relevant EHR target
 * today, so one value serves every real caller; the moment a second
 * EHR-or-practice combination is onboarded this becomes a provider-facing
 * selection made at launch time rather than a build variable. See CLAUDE.md,
 * "Which EHR a client-initiated standalone launch targets", which settles this
 * for both apps — `apps/mobile` reads the same value from
 * `EXPO_PUBLIC_SMART_ISS` and neither app decides it again.
 *
 * Not a credential: an `iss` is a public FHIR base URL and the `aud` the
 * authorization request is bound to, so Vite inlining it costs nothing.
 */
export const SMART_ISS = import.meta.env.VITE_SMART_ISS ?? '';

/**
 * True when the configured origin is not TLS-protected.
 *
 * Covers both schemes this app configures — `ws://` for the two WebSocket
 * origins and `http://` for the three HTTP ones — because the rule they are
 * checked against is one rule, CLAUDE.md's "TLS everywhere". It tested `ws://`
 * alone until TASK-070, which is the same helper `apps/mobile` has always had in
 * its wider form; a second near-identical helper, or a narrower one on this
 * side, is how one origin ends up quietly exempt.
 */
export function isInsecureOrigin(origin: string): boolean {
  return origin.startsWith('ws://') || origin.startsWith('http://');
}
