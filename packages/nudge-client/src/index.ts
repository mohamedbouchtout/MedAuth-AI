/**
 * Reading a clinical nudge off the wire, and dismissing one — shared by
 * `apps/web` and `apps/mobile`.
 *
 * **Scope note:** the nudge payload contract fixed in CLAUDE.md, "The nudge
 * payload — one shape", and track-b-rag's `PATCH /nudges/{nudge_id}/acknowledge`
 * route (TASK-041b). Nothing else. It is not a general HTTP client and it holds
 * no React: both apps subscribe to the nudge socket with their own hook, because
 * the two carry the session token differently — `apps/web` has only the
 * subprotocol carrier, `apps/mobile` uses the `Authorization` header, which
 * CLAUDE.md's "How the JWT reaches a WebSocket endpoint" explains is a platform
 * constraint rather than a choice either app made.
 *
 * It is a separate package from `@medauth/session-client` rather than an
 * addition to it, and that follows that package's own locked scope note: it is
 * the session lifecycle client, and this is one route belonging to a different
 * service, reached with no session credential at all. The `ApiResult` and
 * `ApiFailure` types are imported from there so both apps have one vocabulary
 * for "a call did not produce an answer".
 */

export { parseNudge } from './payload';
export type { DenialRisk, Nudge } from './payload';
export { createNudgesApi } from './acknowledge';
export type { Acknowledgement, FetchLike, NudgesApi } from './acknowledge';
