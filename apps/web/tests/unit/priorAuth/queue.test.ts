/**
 * What a queue row means, and what may be done to it (TASK-072).
 *
 * These are the two judgements the dashboard makes about every row, tested
 * without rendering because they are decisions rather than presentation: which
 * of six states a row is in, and whether asking the payer again is the right
 * move.
 */

import { describe, expect, it } from 'vitest';

import { canResubmit, hasDenialReason, queueEntryView } from '../../../src/priorAuth/queue';
import { aDeniedRequest, aRequest } from '../../support/priorAuth';

describe('queueEntryView', () => {
  it('keeps manual submission apart from a payer refusal and from pending', () => {
    // The three a provider acts on differently: one needs a person, one is over
    // with nothing pending, and one is waiting on nobody yet.
    const manual = queueEntryView(
      aRequest({ status: 'manual-submission-required', rawStatus: 'manual-submission-required' }),
    );
    const errored = queueEntryView(aRequest({ status: 'error', rawStatus: 'error' }));
    const pending = queueEntryView(aRequest());

    expect(manual.kind).toBe('manual');
    expect(errored.kind).toBe('failed');
    expect(pending.kind).toBe('pending');
  });

  it('does not describe a manual request as a failure or as awaiting a payer', () => {
    // Most commercial plans are outside the CMS-0057-F mandate, so this is the
    // ordinary outcome. Telling a provider something went wrong would send them
    // looking for a fix; telling them a payer is deciding would have them
    // waiting on one that was never asked.
    const view = queueEntryView(
      aRequest({ status: 'manual-submission-required', rawStatus: 'manual-submission-required' }),
    );

    expect(view.detail).toMatch(/submit this request/i);
    expect(view.detail).not.toMatch(/failed|error|has not decided/i);
  });

  it('reports an unrecognised status as itself', () => {
    // The status column is free text so a payer-specific state can be added
    // without a migration. Hiding one would under-report a provider's work.
    const view = queueEntryView(aRequest({ status: 'other', rawStatus: 'awaiting-peer-review' }));

    expect(view.kind).toBe('unknown');
    expect(view.label).toBe('awaiting-peer-review');
  });

  it('treats approved and denied as the same kind of fact', () => {
    // Both are the payer having decided. Which way it went is the label's job.
    const approved = queueEntryView(aRequest({ status: 'approved', rawStatus: 'approved' }));
    const denied = queueEntryView(aDeniedRequest());

    expect(approved.kind).toBe('decided');
    expect(denied.kind).toBe('decided');
    expect(approved.label).not.toBe(denied.label);
  });
});

describe('canResubmit', () => {
  it('is true for denied and for error', () => {
    expect(canResubmit(aDeniedRequest())).toBe(true);
    expect(canResubmit(aRequest({ status: 'error', rawStatus: 'error' }))).toBe(true);
  });

  it('is false for a request the payer is still holding', () => {
    // The one direction that must never be got wrong: a payer receiving one
    // request twice may open two reviews of it.
    expect(canResubmit(aRequest({ status: 'submitted', rawStatus: 'submitted' }))).toBe(false);
  });

  it('is false for every other status, including one never submitted', () => {
    // `submittable` is true for a pending request — it has never been sent — so
    // a check that trusted that flag alone would offer a *re*submission on a row
    // that has not been submitted once.
    expect(aRequest().submittable).toBe(true);
    expect(canResubmit(aRequest())).toBe(false);
    expect(canResubmit(aRequest({ status: 'approved', rawStatus: 'approved' }))).toBe(false);
    expect(
      canResubmit(
        aRequest({
          status: 'manual-submission-required',
          rawStatus: 'manual-submission-required',
        }),
      ),
    ).toBe(false);
  });

  it('defers to the service when it says a denied request is not submittable', () => {
    // The rule belongs to the row's owner. Offering a control the service would
    // refuse is a button whose only outcome is a 409.
    expect(canResubmit(aDeniedRequest({ submittable: false }))).toBe(false);
  });
});

describe('hasDenialReason', () => {
  it('is true only for a denial', () => {
    expect(hasDenialReason(aDeniedRequest())).toBe(true);
    expect(hasDenialReason(aRequest({ status: 'error', rawStatus: 'error' }))).toBe(false);
    expect(hasDenialReason(aRequest({ status: 'approved', rawStatus: 'approved' }))).toBe(false);
    expect(hasDenialReason(aRequest())).toBe(false);
  });
});
