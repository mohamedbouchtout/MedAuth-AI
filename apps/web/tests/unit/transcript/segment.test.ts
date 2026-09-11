import { describe, expect, it } from 'vitest';

import { parseSegment } from '../../../src/transcript/segment';
import { transcriptPayload } from '../../support/transcript';

/**
 * The transcript segment parser (TASK-070), against CLAUDE.md's canonical shape.
 *
 * What is asserted is mostly what the parser refuses. A segment this app cannot
 * render must be dropped rather than half-rendered, and a partial must be
 * skipped even though nothing publishes one today — the field exists precisely
 * so a later task can widen the publisher, and a reader that assumed it
 * decorative would render one sentence being re-transcribed several times a
 * second.
 */

describe('parseSegment', () => {
  it('reads the canonical shape', () => {
    const segment = parseSegment(
      transcriptPayload({ resultId: 'abc', text: 'Knee pain.', startTime: 1.5, endTime: 3 }),
    );

    expect(segment).toEqual({
      sessionId: '11111111-1111-4111-8111-111111111111',
      resultId: 'abc',
      text: 'Knee pain.',
      startTime: 1.5,
      endTime: 3,
    });
  });

  /**
   * Nullable because the value is passed through from the transcriber rather
   * than computed, and one that reports no timing is not an error.
   */
  it('accepts absent timings as null', () => {
    const segment = parseSegment(transcriptPayload({ startTime: null, endTime: null }));

    expect(segment?.startTime).toBeNull();
    expect(segment?.endTime).toBeNull();
  });

  it('skips a partial result', () => {
    expect(parseSegment(transcriptPayload({ isPartial: true }))).toBeNull();
  });

  it('drops a frame that is not JSON', () => {
    expect(parseSegment('not json')).toBeNull();
  });

  it('drops a frame that is not an object', () => {
    expect(parseSegment('"a string"')).toBeNull();
    expect(parseSegment('null')).toBeNull();
  });

  /**
   * The publisher drops results carrying no text before they reach the bus, so
   * an empty one should never arrive. Refused anyway: an empty line in a
   * transcript is indistinguishable from a pause the transcriber recorded, and
   * this parser is the last place that can tell them apart.
   */
  it('drops a segment with no text', () => {
    expect(parseSegment(transcriptPayload({ text: '' }))).toBeNull();
  });

  it('drops a segment with no identifiers to key on', () => {
    expect(parseSegment(JSON.stringify({ text: 'Knee pain.' }))).toBeNull();
    expect(parseSegment(JSON.stringify({ text: 'Knee pain.', session_id: 's' }))).toBeNull();
  });

  /**
   * A reader narrows and does not trust: a field absent from the canonical table
   * is not passed through because some transcriber happened to emit it.
   */
  it('ignores fields the contract does not name', () => {
    const segment = parseSegment(
      JSON.stringify({
        session_id: 's',
        result_id: 'r',
        text: 'Knee pain.',
        is_partial: false,
        speaker: 'CLINICIAN',
      }),
    );

    expect(segment).not.toHaveProperty('speaker');
  });

  it('rejects a non-finite timing rather than carrying NaN into the UI', () => {
    const segment = parseSegment(
      JSON.stringify({ session_id: 's', result_id: 'r', text: 'x', start_time: 'soon' }),
    );

    expect(segment?.startTime).toBeNull();
  });
});
