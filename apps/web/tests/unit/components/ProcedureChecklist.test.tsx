import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { parseNudge, type Nudge } from '@medauth/nudge-client';

import { ProcedureChecklist } from '../../../src/components/ProcedureChecklist';
import { fallbackNudgePayload, nudgePayload } from '../../support/nudges';

/**
 * The standing record of what the payer rules flagged during a visit (TASK-070).
 *
 * The nudge overlay is transient — a banner appears and the provider dismisses
 * it. This is what remains, and the two things it must not do are claim a gap
 * has been filled, and present an empty criteria list as "nothing missing".
 */

/**
 * The builder produces the wire object; `parseNudge` takes the frame. Going
 * through the real parser rather than hand-writing a `Nudge` keeps these tests
 * pinned to what actually crosses the socket.
 */
function nudgeFrom(payload: Record<string, unknown>): Nudge {
  const nudge = parseNudge(JSON.stringify(payload));
  if (nudge === null) {
    throw new Error('the fixture did not parse');
  }
  return nudge;
}

describe('with nothing flagged', () => {
  /**
   * Worded as a fact about alerts rather than about coverage. "Nothing needs
   * prior authorization" would be a determination, and no service has made one —
   * a quiet nudge stream and a clean encounter look identical from here.
   */
  it('says no procedure has been flagged, not that none needs authorization', () => {
    render(<ProcedureChecklist flagged={[]} />);

    const text = screen.getByTestId('checklist-empty').textContent ?? '';
    expect(text).toContain('No procedures have been flagged');
    expect(text).not.toMatch(/no prior authorization|nothing (is )?required/i);
  });
});

describe('with procedures flagged', () => {
  it('lists each procedure and what is still undocumented', () => {
    render(
      <ProcedureChecklist
        flagged={[
          nudgeFrom(
            nudgePayload({
              procedure: 'knee MRI',
              cpt_code: '73721',
              missing_criteria: ['six weeks of conservative therapy'],
            }),
          ),
        ]}
      />,
    );

    expect(screen.getByText('knee MRI')).toBeInTheDocument();
    expect(screen.getByText('CPT 73721')).toBeInTheDocument();
    expect(screen.getByText('six weeks of conservative therapy')).toBeInTheDocument();
  });

  /**
   * TASK-044 raises nudges on a keyword that resolved no CPT code, so the entry
   * names a code only when there is one rather than rendering an empty slot.
   */
  it('renders a procedure that resolved no CPT code', () => {
    render(
      <ProcedureChecklist flagged={[nudgeFrom(nudgePayload({ cpt_code: null }))]} />,
    );

    expect(screen.queryByText(/^CPT /)).not.toBeInTheDocument();
  });

  /**
   * The distinction this list exists to carry across the banner's lifetime: on a
   * fallback answer the criteria were never retrieved, so an empty list means
   * *unknown*, not *none*. Rendering it as an empty bullet list would tell a
   * provider the documentation is complete.
   */
  it('says an empty criteria list is unknown, not complete', () => {
    render(<ProcedureChecklist flagged={[nudgeFrom(fallbackNudgePayload())]} />);

    expect(screen.getByText(/No criteria list was available/)).toBeInTheDocument();
  });

  it('keeps entries in the order they were raised', () => {
    render(
      <ProcedureChecklist
        flagged={[
          nudgeFrom(nudgePayload({ nudge_id: 'n-1', procedure: 'knee MRI' })),
          nudgeFrom(nudgePayload({ nudge_id: 'n-2', procedure: 'shoulder MRI' })),
        ]}
      />,
    );

    const items = screen.getByTestId('checklist-items').querySelectorAll('li > p');
    expect([...items].map((item) => item.textContent)).toEqual([
      expect.stringContaining('knee MRI'),
      expect.stringContaining('shoulder MRI'),
    ]);
  });
});
