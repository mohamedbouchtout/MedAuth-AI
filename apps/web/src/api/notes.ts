/**
 * Client for track-a-clinical's note review routes (TASK-032), used by TASK-071.
 *
 * **This stays in `apps/web` rather than becoming a package, and that is the
 * same call TASK-070 made about the transcript parser.** There is one consumer:
 * `apps/mobile` has no note review screen and no task gives it one, so an
 * extraction now would be guessing at the shape a second consumer needs. The
 * trigger is a genuine second consumer, as it was for the three TypeScript
 * packages that do exist.
 *
 * `API_BASE_URL` is track-a-clinical, which owns `clinical_notes`. It is not
 * `FHIR_INTEGRATION_URL`, which owns the EHR write-back in `./ehrNotes` — the
 * note lives here and the chart entry lives there, and the two are different
 * services on different ports.
 *
 * **No credential, deliberately.** Both routes are unauthenticated in v1 and the
 * audit actor is the provider on the `encounters` row, never a claim presented
 * by this app. CLAUDE.md settles that under "Session-scoped routes are keyed on
 * `session_id`", including why `validate_remint_credential` is specifically not
 * reused here: it answers 409 for a completed encounter, and note review happens
 * only on completed encounters.
 *
 * **Nothing here logs.** A note is the densest PHI this app handles — four SOAP
 * sections and a patient's diagnoses — and the request URL carries a session id.
 */

import type { ApiFailure, ApiResult } from '@medauth/session-client';

import { API_BASE_URL } from '../config';

/** Which pass proposed a code, or that a provider did. CLAUDE.md's shape contract. */
export type CodeSource = 'llm-extraction' | 'comprehend-medical' | 'provider-accepted';

/** What an independent pass made of a code. Written by TASK-031. */
export interface CodeValidation {
  source: string;
  confidence: number | null;
  /** Whether the validating source produced this code at or above its threshold. */
  confirmed: boolean;
}

/**
 * One ICD-10-CM or CPT code.
 *
 * Field names are the wire's, not camelCase, and that is deliberate: entries
 * round-trip unchanged through `PATCH`, and a mapping in each direction is a
 * place for a field to be dropped on the way back out. The server validates the
 * shape it receives, so what is read is what is sent.
 */
export interface ExtractedCode {
  code: string;
  display: string | null;
  source: CodeSource;
  /**
   * The proposing source's own score.
   *
   * Always `null` for `llm-extraction` and for `provider-accepted`; the server
   * rejects either carrying one rather than ignoring it.
   */
  confidence: number | null;
  /**
   * `null` means "not checked yet", never "checked and rejected".
   *
   * Permanently `null` on CPT entries, on `comprehend-medical` entries and on
   * `provider-accepted` entries — nothing independent remains to check them
   * against. Rendering it as a rejection is the collapse the contract forbids.
   */
  validation: CodeValidation | null;
}

/** A generated SOAP note as the review screen holds it. */
export interface Note {
  sessionId: string;
  noteId: string;
  soapSubjective: string | null;
  soapObjective: string | null;
  soapAssessment: string | null;
  soapPlan: string | null;
  /**
   * `null` when the extraction pass never answered; `[]` when it ran and found
   * nothing. Two different facts, and the screen must not render the first as
   * "no diagnoses".
   */
  icd10Codes: ExtractedCode[] | null;
  cptCodes: ExtractedCode[] | null;
  generatedAt: string;
  reviewedByProvider: boolean;
  providerEdited: boolean;
  /** The DocumentReference already on the chart, or null. Non-null means filed. */
  ehrDocumentRefId: string | null;
}

/**
 * The body of a `PATCH`, in wire field names, carrying only what changed.
 *
 * **The key set is the message.** CLAUDE.md's "So an editing endpoint needs
 * three states, not two" turns on the difference between an omitted field, an
 * explicit `null` and a list: omitting leaves the column alone, `null` clears
 * it, and a list replaces it. A provider fixing a typo in the plan section must
 * not send `icd10_codes` at all — sending `[]` declares the encounter has no
 * diagnoses, and nothing anywhere would report that as an error.
 *
 * Wire names rather than this app's camelCase, so what `diffNote` builds is
 * literally what goes on the network and no mapping step can add a key back.
 */
export type NotePatch = Partial<{
  soap_subjective: string | null;
  soap_objective: string | null;
  soap_assessment: string | null;
  soap_plan: string | null;
  icd10_codes: ExtractedCode[] | null;
  cpt_codes: ExtractedCode[] | null;
  reviewed_by_provider: boolean;
}>;

export interface NotesApi {
  readNote(sessionId: string): Promise<ApiResult<Note>>;
  /** Applies a partial edit. An empty patch is refused before it is sent. */
  updateNote(sessionId: string, patch: NotePatch): Promise<ApiResult<Note>>;
}

/** The subset of `fetch` this client uses, so tests can supply their own. */
export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

const MALFORMED: ApiFailure = {
  kind: 'malformed',
  message: 'The server returned a response MedAuth AI could not read.',
};

/** Refused locally rather than sent: the server answers 422 for a body that sets nothing. */
const EMPTY_PATCH: ApiFailure = {
  kind: 'malformed',
  message: 'Nothing was changed, so there is nothing to save.',
};

function networkFailure(): ApiFailure {
  // The thrown value is never surfaced: it can carry the request URL, which
  // names a session, and a provider can act on "unreachable" but not on a stack.
  return {
    kind: 'network',
    message: 'MedAuth AI could not reach the server. Check the network connection.',
  };
}

function readError(body: unknown, status: number): ApiFailure {
  const error = (body as { error?: unknown } | null)?.error;
  if (typeof error === 'object' && error !== null) {
    const { code, message } = error as { code?: unknown; message?: unknown };
    if (typeof code === 'string' && typeof message === 'string') {
      return { kind: 'status', status, code, message };
    }
  }
  return { kind: 'status', status, code: 'unknown', message: `The server returned ${status}.` };
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function readValidation(value: unknown): CodeValidation | null {
  if (typeof value !== 'object' || value === null) {
    return null;
  }
  const { source, confidence, confirmed } = value as Record<string, unknown>;
  if (typeof source !== 'string' || typeof confirmed !== 'boolean') {
    return null;
  }
  return {
    source,
    confidence: typeof confidence === 'number' ? confidence : null,
    confirmed,
  };
}

const CODE_SOURCES: readonly CodeSource[] = [
  'llm-extraction',
  'comprehend-medical',
  'provider-accepted',
];

/**
 * Narrow one code entry, or null when it is not one.
 *
 * A `source` outside the vocabulary makes the entry unreadable rather than
 * defaulting to one of the three. The values decide whether a code is rendered
 * as a suggestion or as something the provider is signing, and guessing wrong in
 * that direction puts a machine's proposal in a list a human is about to attest
 * to.
 */
function readCode(value: unknown): ExtractedCode | null {
  if (typeof value !== 'object' || value === null) {
    return null;
  }
  const { code, display, source, confidence, validation } = value as Record<string, unknown>;
  if (typeof code !== 'string' || code === '') {
    return null;
  }
  if (typeof source !== 'string' || !CODE_SOURCES.includes(source as CodeSource)) {
    return null;
  }
  return {
    code,
    display: optionalString(display),
    source: source as CodeSource,
    confidence: typeof confidence === 'number' ? confidence : null,
    validation: readValidation(validation),
  };
}

/**
 * Narrow a code list, preserving the null/empty distinction.
 *
 * `undefined` here means the field was absent or unreadable, which the caller
 * treats as a malformed response rather than as either of the two real answers.
 */
function readCodes(value: unknown): ExtractedCode[] | null | undefined {
  if (value === null) {
    return null;
  }
  if (!Array.isArray(value)) {
    return undefined;
  }
  const codes: ExtractedCode[] = [];
  for (const entry of value) {
    const code = readCode(entry);
    if (code === null) {
      return undefined;
    }
    codes.push(code);
  }
  return codes;
}

function readNoteBody(body: unknown): Note | null {
  const data = (body as { data?: unknown } | null)?.data;
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  const fields = data as Record<string, unknown>;
  const sessionId = fields.session_id;
  const noteId = fields.note_id;
  const generatedAt = fields.generated_at;
  const reviewed = fields.reviewed_by_provider;
  const edited = fields.provider_edited;
  if (
    typeof sessionId !== 'string' ||
    typeof noteId !== 'string' ||
    typeof generatedAt !== 'string' ||
    typeof reviewed !== 'boolean' ||
    typeof edited !== 'boolean'
  ) {
    return null;
  }

  const icd10Codes = readCodes(fields.icd10_codes);
  const cptCodes = readCodes(fields.cpt_codes);
  if (icd10Codes === undefined || cptCodes === undefined) {
    return null;
  }

  return {
    sessionId,
    noteId,
    soapSubjective: optionalString(fields.soap_subjective),
    soapObjective: optionalString(fields.soap_objective),
    soapAssessment: optionalString(fields.soap_assessment),
    soapPlan: optionalString(fields.soap_plan),
    icd10Codes,
    cptCodes,
    generatedAt,
    reviewedByProvider: reviewed,
    providerEdited: edited,
    ehrDocumentRefId: optionalString(fields.ehr_document_ref_id),
  };
}

export function createNotesApi(
  baseUrl: string = API_BASE_URL,
  fetchImpl: FetchLike = (url, init) => fetch(url, init),
): NotesApi {
  async function call(sessionId: string, init?: RequestInit): Promise<ApiResult<Note>> {
    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}/notes/${sessionId}`, init);
    } catch {
      return { ok: false, failure: networkFailure() };
    }

    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      // Only fatal on the success path: an error status still says what
      // happened without a parseable body, which is what a proxy 502 looks like.
      if (response.ok) {
        return { ok: false, failure: MALFORMED };
      }
    }

    if (!response.ok) {
      return { ok: false, failure: readError(body, response.status) };
    }

    const note = readNoteBody(body);
    return note === null ? { ok: false, failure: MALFORMED } : { ok: true, value: note };
  }

  return {
    readNote(sessionId) {
      return call(sessionId);
    },

    updateNote(sessionId, patch) {
      if (Object.keys(patch).length === 0) {
        return Promise.resolve({ ok: false, failure: EMPTY_PATCH });
      }
      return call(sessionId, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
    },
  };
}

/** The client the review screen uses when none is injected. */
export const notesApi = createNotesApi();
