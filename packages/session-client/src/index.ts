/**
 * The session lifecycle client, shared by `apps/web` and `apps/mobile`.
 *
 * **Why this is a package.** It was `apps/mobile/src/api/` until TASK-042, when
 * `apps/web`'s nudge overlay became the second client needing to re-mint a
 * session token before opening a socket. That is the same trigger, and the same
 * argument, that produced `packages/api-envelope` when track-b-rag copied
 * track-a-clinical's envelope and `packages/session-auth` when nudge-service
 * needed audio-ingestion's validator: two hand-maintained copies of one
 * credential path diverge, not on purpose, but because a fix lands in whichever
 * file the person had open.
 *
 * The hazard here is specific rather than stylistic. `startVisit` and
 * `remintToken` are one keystroke apart in a client's editor and produce
 * silently different outcomes — CLAUDE.md's "Re-mint for the same `session_id`"
 * bullet lists what a `/sessions/start` used as a refresh breaks, and nothing
 * along that path errors. Both apps calling the same two named methods is worth
 * more than either app's copy being locally tidy.
 *
 * **Scope note:** the session lifecycle endpoints of `track-a-clinical`
 * (TASK-006, TASK-006b), the token-freshness check that decides when to call the
 * re-mint one, and how a session token is presented — the `Authorization`
 * header on that call, and the WebSocket subprotocol carrier a browser has to
 * use instead. It is not a general HTTP client, and no other service's
 * routes belong here — `apps/web`'s nudge acknowledge call lives in that app,
 * because it is a track-b-rag route with no session credential at all.
 */

export { TOKEN_REFRESH_SKEW_MS, expiresAtMs, isNearExpiry } from './jwt';
export { SESSION_SUBPROTOCOL, sessionSubprotocols } from './subprotocols';
export { createSessionsApi } from './sessions';
export type {
  ApiFailure,
  ApiResult,
  FetchLike,
  Session,
  SessionsApi,
  StartVisitInput,
} from './sessions';
