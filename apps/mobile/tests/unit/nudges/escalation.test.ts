/**
 * `shouldBuzz` is one line, and this suite exists because of what that line must
 * not become.
 *
 * The two properties are pinned separately: that `haptic` decides, and that
 * `denialRisk` does not. A single test on the happy path would pass just as
 * happily against `nudge.haptic || nudge.denialRisk === 'high'`, which is the
 * exact change CLAUDE.md's "The nudge payload — one shape" forbids and the one a
 * later reader is most likely to make, since the two fields look interchangeable
 * in a payload where they usually agree.
 */

import { parseNudge, type Nudge } from '@medauth/nudge-client';
import { fallbackNudgePayload, nudgePayload } from '@medauth/nudge-client/testing';

import { shouldBuzz } from '../../../src/nudges/escalation';

function parsed(payload: Record<string, unknown>): Nudge {
  const nudge = parseNudge(JSON.stringify(payload));
  if (nudge === null) {
    throw new Error('fixture is not a valid nudge payload');
  }
  return nudge;
}

describe('shouldBuzz', () => {
  it('is true when the emitter set haptic', () => {
    expect(shouldBuzz(parsed(nudgePayload()))).toBe(true);
  });

  it('is false when the emitter withheld haptic, whatever the risk', () => {
    expect(shouldBuzz(parsed(fallbackNudgePayload()))).toBe(false);
  });

  /**
   * High risk is not the trigger. The fallback answer reports `high` honestly —
   * the requirement is genuinely unverified — while withholding the escalation,
   * so an outage cannot spend the credibility the buzz depends on.
   */
  it.each(['low', 'medium', 'high'] as const)(
    'follows haptic and not denial_risk (%s)',
    (denialRisk) => {
      expect(shouldBuzz(parsed(nudgePayload({ denial_risk: denialRisk, haptic: false })))).toBe(
        false,
      );
      expect(shouldBuzz(parsed(nudgePayload({ denial_risk: denialRisk, haptic: true })))).toBe(true);
    },
  );
});
