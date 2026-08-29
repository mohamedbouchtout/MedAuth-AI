import { describe, expect, it } from 'vitest';

import { parseNudge } from '../../../src/nudges/payload';
import { nudgePayload } from '../../support/nudges';

function frame(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify(nudgePayload(overrides));
}

describe('parseNudge', () => {
  it('reads the wire shape from CLAUDE.md', () => {
    expect(parseNudge(frame())).toEqual({
      type: 'PAYER_RULE_ALERT',
      nudgeId: '0b7f0000-0000-4000-8000-000000000001',
      procedure: 'knee MRI',
      cptCode: '73721',
      message:
        'Prior authorization required for knee MRI. Still undocumented: six weeks of therapy.',
      missingCriteria: ['six weeks of conservative therapy'],
      denialRisk: 'high',
      haptic: true,
    });
  });

  it('accepts a null cpt_code', () => {
    // TASK-044 nudges on a keyword that resolved no CPT code. The contract has
    // been nullable since TASK-040 precisely so this does not need a migration.
    expect(parseNudge(frame({ cpt_code: null }))?.cptCode).toBeNull();
  });

  it('treats an absent cpt_code as no code rather than as malformed', () => {
    const payload = nudgePayload();
    delete payload.cpt_code;
    expect(parseNudge(JSON.stringify(payload))?.cptCode).toBeNull();
  });

  it('keeps an empty missing_criteria rather than discarding the nudge', () => {
    // Empty means the criteria are unknown on a fallback answer, which is
    // information the provider needs — not a reason to drop the alert.
    expect(parseNudge(frame({ missing_criteria: [] }))?.missingCriteria).toEqual([]);
  });

  it.each([
    ['not JSON at all', 'this is not json'],
    ['a JSON scalar', '"just a string"'],
    ['JSON null', 'null'],
  ])('drops %s', (_name, payload) => {
    expect(parseNudge(payload)).toBeNull();
  });

  it.each([
    ['a missing nudge_id', { nudge_id: undefined }],
    ['a non-string procedure', { procedure: 42 }],
    ['a denial_risk outside the vocabulary', { denial_risk: 'catastrophic' }],
    ['a non-boolean haptic', { haptic: 'yes' }],
    ['missing_criteria that is not a list of strings', { missing_criteria: [1, 2] }],
    ['a numeric cpt_code', { cpt_code: 73721 }],
  ])('drops a payload with %s', (_name, overrides) => {
    const payload = nudgePayload(overrides as Record<string, unknown>);
    if ((overrides as Record<string, unknown>).nudge_id === undefined) {
      delete payload.nudge_id;
    }
    expect(parseNudge(JSON.stringify(payload))).toBeNull();
  });
});
