/**
 * Reading a transcript segment off the wire (TASK-070).
 *
 * The shape is canonical in CLAUDE.md, "The transcript segment payload — one
 * shape", fixed by TASK-041d precisely because this parser was about to be the
 * fourth participant in a contract that had one writer and two readers, each of
 * which had hand-rolled its own `json.loads(payload)["text"]`. This module
 * implements that section and does not restate it.
 *
 * **It stays in `apps/web` rather than going into a package, and that is the
 * extraction rule applied rather than skipped.** `apps/mobile` does not display
 * a live transcript, so there is exactly one TypeScript consumer of this shape
 * today; extracting now would mean guessing at what a second one needs. The
 * trigger is a genuine second consumer — the same trigger that produced
 * `@medauth/session-client`, `@medauth/nudge-client` and, in this task,
 * `@medauth/fhir-client`.
 *
 * **A reader narrows, it does not trust.** Every field this module returns is
 * checked; anything else on the object is ignored rather than passed through,
 * and no field absent from the canonical table is assumed to exist because some
 * transcriber happened to emit it.
 *
 * **`text` is PHI — it is what was said in a clinical encounter.** Nothing here
 * logs, and a segment that fails to parse is dropped with no record of its
 * content anywhere. That is the same rule every module on this path already
 * states, and this is exactly where it would otherwise be forgotten.
 */

export interface TranscriptSegment {
  /** The encounter this segment belongs to. Repeated in the payload by design. */
  sessionId: string;
  /**
   * Transcribe Medical's identifier for the utterance.
   *
   * The de-duplication key. Two utterances can legitimately have identical text,
   * so a reader that needs idempotency keys on this and never on the words.
   */
  resultId: string;
  /** What was said. Never empty — the publisher drops results carrying no text. */
  text: string;
  /** Seconds from the start of the stream, or null when the transcriber gave none. */
  startTime: number | null;
  /** The same, for the end of the utterance. */
  endTime: number | null;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Parse one relayed frame, or null when it is not a segment this app can render.
 *
 * Null covers a frame that is not JSON, is not an object, or carries no usable
 * `text` — and, deliberately, a partial result.
 *
 * **Partials are skipped rather than assumed absent.** `publish_segment` drops
 * them before they reach the bus, so `is_partial` is always false on this
 * channel today and nothing here would see one. The check is here because the
 * field exists precisely so a later task can widen the publisher without
 * changing this shape — and on the day that happens, a reader that assumed the
 * field decorative would render one sentence being re-transcribed several times
 * a second. Both existing consumers skip partials for the same reason.
 */
export function parseSegment(frame: string): TranscriptSegment | null {
  let payload: unknown;
  try {
    payload = JSON.parse(frame);
  } catch {
    return null;
  }
  if (typeof payload !== 'object' || payload === null) {
    return null;
  }

  const record = payload as Record<string, unknown>;
  if (record.is_partial === true) {
    return null;
  }

  const { session_id: sessionId, result_id: resultId, text } = record;
  if (typeof text !== 'string' || text === '') {
    return null;
  }
  if (typeof sessionId !== 'string' || typeof resultId !== 'string') {
    return null;
  }

  return {
    sessionId,
    resultId,
    text,
    startTime: optionalNumber(record.start_time),
    endTime: optionalNumber(record.end_time),
  };
}
