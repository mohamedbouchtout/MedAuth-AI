/**
 * Fixtures for the transcript stream's tests.
 *
 * The payload builder produces the wire shape from CLAUDE.md, "The transcript
 * segment payload — one shape", in snake_case, because that is what crosses the
 * socket. It is written here rather than derived from `src/transcript/segment`
 * on purpose: a fixture built by the parser under test can only ever prove the
 * parser agrees with itself.
 *
 * The fake socket comes from `./nudges` rather than being defined again — both
 * streams open the same kind of connection with the same credential carrier, and
 * a second fake is a second thing to keep in step.
 */

export { FakeWebSocket, installFakeWebSocket, tokenExpiringAt } from './nudges';

export interface SegmentOverrides {
  sessionId?: string;
  resultId?: string;
  text?: string;
  isPartial?: boolean;
  startTime?: number | null;
  endTime?: number | null;
}

/** One stabilized transcript segment, as `encode_segment` publishes it. */
export function transcriptPayload(overrides: SegmentOverrides = {}): string {
  return JSON.stringify({
    session_id: overrides.sessionId ?? '11111111-1111-4111-8111-111111111111',
    result_id: overrides.resultId ?? 'result-1',
    text: overrides.text ?? 'Patient reports right knee pain for about six weeks.',
    is_partial: overrides.isPartial ?? false,
    start_time: overrides.startTime === undefined ? 12.34 : overrides.startTime,
    end_time: overrides.endTime === undefined ? 16.78 : overrides.endTime,
  });
}
