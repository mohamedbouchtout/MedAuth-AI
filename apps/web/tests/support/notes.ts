/**
 * Note fixtures for TASK-071's tests.
 *
 * The wire shapes here are CLAUDE.md's "Extracted clinical codes — one JSON
 * shape" and `docs/api/track-a-clinical.yaml`, not shapes invented for the
 * tests: the interesting cases are the ones the contract draws distinctions
 * between — `null` against `[]`, and the three `source` values — so a fixture
 * that blurred them would prove nothing.
 */

import type { ApiResult } from '@medauth/session-client';
import { vi, type Mock } from 'vitest';

import type { EhrNotesApi } from '../../src/api/ehrNotes';
import type { ExtractedCode, Note, NotePatch, NotesApi } from '../../src/api/notes';

export const SESSION_ID = '11111111-1111-4111-8111-111111111111';

export const llmCode: ExtractedCode = {
  code: 'M17.11',
  display: 'Unilateral primary osteoarthritis, right knee',
  source: 'llm-extraction',
  confidence: null,
  validation: { source: 'comprehend-medical', confidence: 0.94, confirmed: true },
};

/** A code the LLM never proposed. A suggestion, never a stated diagnosis. */
export const suggestedCode: ExtractedCode = {
  code: 'M25.561',
  display: 'Pain in right knee',
  source: 'comprehend-medical',
  confidence: 0.88,
  validation: null,
};

export function aNote(overrides: Partial<Note> = {}): Note {
  return {
    sessionId: SESSION_ID,
    noteId: '22222222-2222-4222-8222-222222222222',
    soapSubjective: 'Right knee pain for six weeks.',
    soapObjective: 'Tenderness over the medial joint line.',
    soapAssessment: 'Likely medial meniscus injury.',
    soapPlan: 'MRI right knee.',
    icd10Codes: [llmCode, suggestedCode],
    cptCodes: [],
    generatedAt: '2026-09-11T10:00:00Z',
    reviewedByProvider: false,
    providerEdited: false,
    ehrDocumentRefId: null,
    ...overrides,
  };
}

/**
 * A `NotesApi` whose two methods are spies.
 *
 * The mock generics are spelled out rather than left to inference so the fake
 * still has to satisfy the real interface — a spy typed loosely would let a
 * fixture drift from the client it stands in for without anything noticing.
 */
export interface FakeNotes extends NotesApi {
  readNote: Mock<(sessionId: string) => Promise<ApiResult<Note>>>;
  updateNote: Mock<(sessionId: string, patch: NotePatch) => Promise<ApiResult<Note>>>;
}

/** A client that serves one note and echoes edits back as the server would. */
export function notesServing(note: Note = aNote()): FakeNotes {
  let held = note;
  return {
    readNote: vi.fn<NotesApi['readNote']>(() =>
      Promise.resolve({ ok: true as const, value: held }),
    ),
    updateNote: vi.fn<NotesApi['updateNote']>((sessionId, patch) => {
      void sessionId;
      const touchesContent = Object.keys(patch).some((key) => key !== 'reviewed_by_provider');
      held = {
        ...held,
        ...(patch.soap_subjective === undefined ? {} : { soapSubjective: patch.soap_subjective }),
        ...(patch.soap_objective === undefined ? {} : { soapObjective: patch.soap_objective }),
        ...(patch.soap_assessment === undefined ? {} : { soapAssessment: patch.soap_assessment }),
        ...(patch.soap_plan === undefined ? {} : { soapPlan: patch.soap_plan }),
        ...(patch.icd10_codes === undefined ? {} : { icd10Codes: patch.icd10_codes }),
        ...(patch.cpt_codes === undefined ? {} : { cptCodes: patch.cpt_codes }),
        ...(patch.reviewed_by_provider === undefined
          ? {}
          : { reviewedByProvider: patch.reviewed_by_provider }),
        providerEdited: held.providerEdited || touchesContent,
        // The server answers with a fresh row; this stands in for the change
        // that makes the screen adopt it as its new baseline.
        generatedAt: held.generatedAt,
      };
      return Promise.resolve({ ok: true as const, value: held });
    }),
  };
}

/** A client whose read fails with one envelope error, for the 404 branches. */
export function notesFailing(status: number, code: string, message = 'nope'): FakeNotes {
  const failure = { kind: 'status' as const, status, code, message };
  return {
    readNote: vi.fn<NotesApi['readNote']>(() => Promise.resolve({ ok: false as const, failure })),
    updateNote: vi.fn<NotesApi['updateNote']>(() =>
      Promise.resolve({ ok: false as const, failure }),
    ),
  };
}

export interface FakeEhrNotes extends EhrNotesApi {
  writeNote: Mock<EhrNotesApi['writeNote']>;
}

export function ehrNotesThat(result: Awaited<ReturnType<EhrNotesApi['writeNote']>>): FakeEhrNotes {
  return {
    writeNote: vi.fn<EhrNotesApi['writeNote']>(() => Promise.resolve(result)),
  };
}
