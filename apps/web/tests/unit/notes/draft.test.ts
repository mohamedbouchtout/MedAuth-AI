/**
 * What a save sends (TASK-071).
 *
 * The central assertion in this file is a *negative* one: a field the provider
 * did not touch produces no key at all. CLAUDE.md's "So an editing endpoint
 * needs three states, not two" names the direction that fails — a provider
 * fixing a typo in the plan section who also sent `icd10_codes: []` would have
 * declared the encounter has no diagnoses, and no layer anywhere reports that as
 * an error. `toHaveProperty` is what catches it; a deep-equality check on the
 * patch would pass just as happily against a key holding `undefined`.
 */

import { describe, expect, it } from 'vitest';

import {
  acceptSuggestion,
  addCode,
  diffNote,
  draftOf,
  hasEdits,
  removeCode,
} from '../../../src/notes/draft';
import { aNote, llmCode, suggestedCode } from '../../support/notes';

describe('diffing a note', () => {
  it('sends nothing at all when nothing changed', () => {
    const draft = draftOf(aNote());

    expect(diffNote(draft, draft)).toEqual({});
    expect(hasEdits(draft, draft)).toBe(false);
  });

  it('sends only the section that changed', () => {
    const original = draftOf(aNote());
    const edited = { ...original, soapPlan: 'MRI left knee.' };

    const patch = diffNote(original, edited);

    expect(patch).toEqual({ soap_plan: 'MRI left knee.' });
  });

  /**
   * The case this whole contract exists for. A note whose codes were never
   * extracted, edited in one text section: the code keys must be absent, not
   * `null` and not `[]`. `null` would clear a column nobody touched and `[]`
   * would assert the encounter has no diagnoses.
   */
  it('omits the code lists entirely when they were not touched', () => {
    const original = draftOf(aNote({ icd10Codes: null, cptCodes: null }));
    const edited = { ...original, soapSubjective: 'Left knee pain for six weeks.' };

    const patch = diffNote(original, edited);

    expect(patch).not.toHaveProperty('icd10_codes');
    expect(patch).not.toHaveProperty('cpt_codes');
    expect(Object.keys(patch)).toEqual(['soap_subjective']);
  });

  /** `null` and `[]` are different answers, so moving between them is an edit. */
  it('treats clearing an extracted list as a change', () => {
    const original = draftOf(aNote({ icd10Codes: [llmCode] }));
    const edited = { ...original, icd10Codes: [] };

    expect(diffNote(original, edited)).toEqual({ icd10_codes: [] });
  });

  it('does not treat a null list as equal to an empty one', () => {
    const original = draftOf(aNote({ icd10Codes: null }));
    const edited = { ...original, icd10Codes: [] };

    expect(diffNote(original, edited)).toEqual({ icd10_codes: [] });
  });
});

describe('editing codes', () => {
  /**
   * Accepting is what makes a suggestion claimable by TASK-060 and sendable to a
   * chart by TASK-053. It rewrites the entry in place — a code is one entry
   * whichever pass found it — and drops both the score and the validation, which
   * the server rejects rather than ignores.
   */
  it('accepting a suggestion rewrites it as provider-accepted with no score', () => {
    const codes = acceptSuggestion([llmCode, suggestedCode], suggestedCode.code);

    expect(codes).toHaveLength(2);
    expect(codes?.[1]).toEqual({
      code: suggestedCode.code,
      display: suggestedCode.display,
      source: 'provider-accepted',
      confidence: null,
      validation: null,
    });
  });

  it('leaves an extracted code alone, since only a suggestion can be accepted', () => {
    const codes = acceptSuggestion([llmCode, suggestedCode], llmCode.code);

    expect(codes?.[0]).toEqual(llmCode);
  });

  it('removes a code by its own value', () => {
    expect(removeCode([llmCode, suggestedCode], llmCode.code)).toEqual([suggestedCode]);
  });

  /** A code a provider types is documentation, so it carries their source and no score. */
  it('adds a typed code as provider-accepted', () => {
    const codes = addCode(null, 'm17.12', 'Left knee');

    expect(codes).toEqual([
      {
        code: 'M17.12',
        display: 'Left knee',
        source: 'provider-accepted',
        confidence: null,
        validation: null,
      },
    ]);
  });

  it('does not add a second entry for a code the note already carries', () => {
    expect(addCode([llmCode], 'm17.11')).toEqual([llmCode]);
  });

  it('ignores an empty entry', () => {
    expect(addCode([llmCode], '   ')).toEqual([llmCode]);
  });

  it('leaves a null list null when there is nothing to accept or remove in it', () => {
    expect(acceptSuggestion(null, 'M17.11')).toBeNull();
    expect(removeCode(null, 'M17.11')).toBeNull();
  });
});
