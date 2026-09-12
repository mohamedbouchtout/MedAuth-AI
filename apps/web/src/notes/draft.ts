/**
 * What the provider has changed, and what may therefore be sent (TASK-071).
 *
 * This is a pure module and separate from the screen for the same reason
 * `visitPhase` is: the requirement it carries is a property of a mapping, and a
 * mapping can be tested directly. Left inline in a component it would be a
 * property of whichever branches someone remembered to write.
 *
 * **The requirement: an untouched field is not sent at all.** CLAUDE.md's "So an
 * editing endpoint needs three states, not two" turns on the difference between
 * an omitted key, an explicit `null` and a value — omitting leaves the column
 * alone, `null` clears it, a value replaces it. The direction that fails is the
 * dangerous one: a provider correcting a typo in the plan section who also sends
 * `icd10_codes: []` has declared that the encounter has no diagnoses, and
 * nothing anywhere reports that as an error. So `diffNote` emits a key only when
 * that field actually differs, and a note whose codes were never touched
 * produces a patch with no code keys in it.
 *
 * Nothing here logs: every value passing through is clinical content.
 */

import type { ExtractedCode, Note, NotePatch } from '../api/notes';

/** The editable half of a note, as the screen holds it while it is being edited. */
export interface NoteDraft {
  soapSubjective: string | null;
  soapObjective: string | null;
  soapAssessment: string | null;
  soapPlan: string | null;
  icd10Codes: ExtractedCode[] | null;
  cptCodes: ExtractedCode[] | null;
}

/** Take the editable fields off a loaded note. The rest is server-owned. */
export function draftOf(note: Note): NoteDraft {
  return {
    soapSubjective: note.soapSubjective,
    soapObjective: note.soapObjective,
    soapAssessment: note.soapAssessment,
    soapPlan: note.soapPlan,
    icd10Codes: note.icd10Codes,
    cptCodes: note.cptCodes,
  };
}

function sameCode(left: ExtractedCode, right: ExtractedCode): boolean {
  return (
    left.code === right.code &&
    left.display === right.display &&
    left.source === right.source &&
    left.confidence === right.confidence &&
    left.validation?.source === right.validation?.source &&
    left.validation?.confidence === right.validation?.confidence &&
    left.validation?.confirmed === right.validation?.confirmed
  );
}

/**
 * Whether two code lists are the same answer.
 *
 * `null` and `[]` are never the same: one says the extraction pass never
 * answered and the other says it ran and found nothing. Treating them as equal
 * here would make an explicit clearing invisible to the diff, and treating them
 * as different when both are `null` would send a key for a field nobody touched.
 */
function sameCodes(left: ExtractedCode[] | null, right: ExtractedCode[] | null): boolean {
  if (left === null || right === null) {
    return left === right;
  }
  return (
    left.length === right.length && left.every((entry, index) => sameCode(entry, right[index]!))
  );
}

/**
 * The patch carrying exactly what changed, and nothing else.
 *
 * An unchanged note produces `{}`, which the client refuses to send rather than
 * letting the server answer 422 for a body that sets no fields.
 */
export function diffNote(original: NoteDraft, edited: NoteDraft): NotePatch {
  const patch: NotePatch = {};
  if (original.soapSubjective !== edited.soapSubjective) {
    patch.soap_subjective = edited.soapSubjective;
  }
  if (original.soapObjective !== edited.soapObjective) {
    patch.soap_objective = edited.soapObjective;
  }
  if (original.soapAssessment !== edited.soapAssessment) {
    patch.soap_assessment = edited.soapAssessment;
  }
  if (original.soapPlan !== edited.soapPlan) {
    patch.soap_plan = edited.soapPlan;
  }
  if (!sameCodes(original.icd10Codes, edited.icd10Codes)) {
    patch.icd10_codes = edited.icd10Codes;
  }
  if (!sameCodes(original.cptCodes, edited.cptCodes)) {
    patch.cpt_codes = edited.cptCodes;
  }
  return patch;
}

/** True when there is something to save. */
export function hasEdits(original: NoteDraft, edited: NoteDraft): boolean {
  return Object.keys(diffNote(original, edited)).length > 0;
}

/**
 * A provider accepting a machine suggestion, which is what makes it claimable.
 *
 * The entry is mutated in place in the list rather than appended alongside:
 * CLAUDE.md's shape contract is explicit that a code is one entry whichever pass
 * found it. `confidence` and `validation` are both dropped, and neither is an
 * oversight — Comprehend's score measured its own linkage rather than the
 * provider's judgement, and carrying it forward would attach a machine's
 * uncertainty to a human's decision where no later reader could tell which of
 * the two the number described. The server rejects an entry that keeps either.
 */
export function acceptSuggestion(
  codes: ExtractedCode[] | null,
  code: string,
): ExtractedCode[] | null {
  if (codes === null) {
    return null;
  }
  return codes.map((entry) =>
    entry.code === code && entry.source === 'comprehend-medical'
      ? { ...entry, source: 'provider-accepted' as const, confidence: null, validation: null }
      : entry,
  );
}

/** Drop a code the provider does not want on the note. */
export function removeCode(codes: ExtractedCode[] | null, code: string): ExtractedCode[] | null {
  if (codes === null) {
    return null;
  }
  return codes.filter((entry) => entry.code !== code);
}

/**
 * Add a code the provider typed, as documentation rather than as a suggestion.
 *
 * `provider-accepted` is the only source a human ever writes, and it carries no
 * confidence: a human acceptance is a fact, not a probability. A `null` list
 * becomes a one-entry list, which is a provider stating a diagnosis rather than
 * this app inferring `[]` from an extraction pass that never answered — the
 * collapse the shape contract exists to prevent is an *inferred* empty list, not
 * a provider's own edit.
 *
 * A code already on the note is returned unchanged, because a code is one entry.
 */
export function addCode(
  codes: ExtractedCode[] | null,
  code: string,
  display: string | null = null,
): ExtractedCode[] {
  const existing = codes ?? [];
  const normalised = code.trim().toUpperCase();
  if (normalised === '' || existing.some((entry) => entry.code === normalised)) {
    return existing;
  }
  return [
    ...existing,
    {
      code: normalised,
      display,
      source: 'provider-accepted',
      confidence: null,
      validation: null,
    },
  ];
}
