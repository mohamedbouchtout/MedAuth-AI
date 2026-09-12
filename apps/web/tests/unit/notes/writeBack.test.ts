/**
 * Which of four states the chart write is in, before anything is pressed
 * (TASK-071).
 *
 * The requirement under test is that a control whose only possible outcome is an
 * error is never offered, and that the reasons stay distinguishable from each
 * other — two genuinely different situations get two legible states rather than
 * one collapsed into the other or one silently hidden.
 */

import { describe, expect, it } from 'vitest';

import { writeBackState, writeBackView, type WriteAttempt } from '../../../src/notes/writeBack';

const LAUNCH = 'launch-7';
const ENCOUNTER = 'Encounter/7';

describe('what the note allows', () => {
  it('is available with a launch and a chart entry', () => {
    expect(
      writeBackState({ ehrDocumentRefId: null, launchId: LAUNCH, ehrEncounterId: ENCOUNTER }),
    ).toEqual({ kind: 'available' });
  });

  /**
   * A visit started outside a SMART launch has no chart entry, so
   * `POST /fhir/notes` could only ever answer 422. Said before the press.
   */
  it('is unavailable, and says why, for a visit with no chart entry', () => {
    expect(
      writeBackState({ ehrDocumentRefId: null, launchId: LAUNCH, ehrEncounterId: null }),
    ).toEqual({ kind: 'not-linked' });
  });

  it('is unavailable, differently, for a page holding no launch', () => {
    expect(
      writeBackState({ ehrDocumentRefId: null, launchId: null, ehrEncounterId: ENCOUNTER }),
    ).toEqual({ kind: 'no-launch' });
  });

  /**
   * After a reload this page has neither, and the missing launch is the one the
   * provider can act on — launching again restores both. Reporting "no chart
   * entry" there would be a guess dressed as a fact.
   */
  it('reports the missing launch first when both are gone', () => {
    expect(writeBackState({ ehrDocumentRefId: null, launchId: null, ehrEncounterId: null })).toEqual(
      { kind: 'no-launch' },
    );
  });

  /** Server-side truth, so it still holds after a reload when the rest is gone. */
  it('reports a filed note ahead of everything else', () => {
    expect(
      writeBackState({ ehrDocumentRefId: 'DocumentReference/9', launchId: null, ehrEncounterId: null }),
    ).toEqual({ kind: 'filed', documentId: 'DocumentReference/9' });
  });
});

describe('layering an attempt over it', () => {
  const available = writeBackState({
    ehrDocumentRefId: null,
    launchId: LAUNCH,
    ehrEncounterId: ENCOUNTER,
  });

  it('shows the standing state when nothing has been pressed', () => {
    expect(writeBackView(available, { kind: 'idle' })).toEqual({ kind: 'available' });
  });

  /**
   * The document exists on the chart and this system did not record it. Nothing
   * may put the button back: a second press files a second copy of one visit's
   * note. Asserted against a *filed* note too, so no later state can outrank it.
   */
  it('never leaves the action available after an unrecorded write', () => {
    const attempt: WriteAttempt = { kind: 'unrecorded', message: 'created DocumentReference/9' };

    expect(writeBackView(available, attempt)).toEqual({
      kind: 'unrecorded',
      message: 'created DocumentReference/9',
    });
    expect(
      writeBackView(
        writeBackState({ ehrDocumentRefId: 'DocumentReference/9', launchId: LAUNCH, ehrEncounterId: ENCOUNTER }),
        attempt,
      ),
    ).toEqual({ kind: 'unrecorded', message: 'created DocumentReference/9' });
  });

  /** A 409 returns no document id, and inventing one is the fabrication this repo refuses. */
  it('reports a 409 as filed with no document id', () => {
    expect(writeBackView(available, { kind: 'filed' })).toEqual({ kind: 'filed', documentId: null });
  });

  it('prefers the note’s own document id over a 409 with none', () => {
    const filed = writeBackState({
      ehrDocumentRefId: 'DocumentReference/9',
      launchId: LAUNCH,
      ehrEncounterId: ENCOUNTER,
    });

    expect(writeBackView(filed, { kind: 'filed' })).toEqual({
      kind: 'filed',
      documentId: 'DocumentReference/9',
    });
  });

  it('adopts a 422 as the standing reason rather than as an error', () => {
    expect(writeBackView(available, { kind: 'not-linked' })).toEqual({ kind: 'not-linked' });
  });

  it('reports a transient failure where the action was genuinely available', () => {
    expect(writeBackView(available, { kind: 'failed', message: 'unreachable' })).toEqual({
      kind: 'failed',
      message: 'unreachable',
    });
  });

  /**
   * A failure against an unavailable action is not the useful thing to say — the
   * standing reason the write cannot happen is.
   */
  it('keeps the standing reason when the action was not available', () => {
    const noLaunch = writeBackState({
      ehrDocumentRefId: null,
      launchId: null,
      ehrEncounterId: ENCOUNTER,
    });

    expect(writeBackView(noLaunch, { kind: 'failed', message: 'unreachable' })).toEqual({
      kind: 'no-launch',
    });
  });

  it('shows progress while a write is in flight', () => {
    expect(writeBackView(available, { kind: 'writing' })).toEqual({ kind: 'writing' });
  });
});
