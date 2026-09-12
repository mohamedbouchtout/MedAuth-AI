/**
 * The note review and edit screen (TASK-071).
 *
 * A provider reads the SOAP note TASK-030 generated, corrects it, accepts or
 * rejects the codes that were extracted, attests that they have reviewed it, and
 * files it to the patient's chart.
 *
 * Four things here are requirements rather than presentation choices.
 *
 * **An untouched field is never sent.** The patch is built by `diffNote`, which
 * emits a key only for a field that actually differs. CLAUDE.md's "So an editing
 * endpoint needs three states, not two" is explicit about which direction fails:
 * a provider fixing a typo in the plan section who also sent `icd10_codes: []`
 * would have declared the encounter has no diagnoses, and nothing anywhere
 * reports that as an error.
 *
 * **Loading this screen attests to nothing.** `reviewed_by_provider` is sent
 * only when the provider presses the button that sends it. It records a human
 * having read the note, and setting it on a page load would make it record that
 * a screen was opened — a distinction an auditor cannot recover afterwards.
 *
 * **A machine suggestion is visibly a suggestion until a provider accepts it.**
 * That lives in `CodeList` and in `acceptSuggestion`; this screen only wires
 * them to the note.
 *
 * **The EHR write is never offered as a control that can only fail.**
 * `writeBackState` decides which of four states this note is in before anything
 * is pressed, and each one says what is true and what would change it — see that
 * module for why hiding the control is not the alternative.
 *
 * Nothing here logs. The note is the densest PHI this app handles.
 */

import { useCallback, useMemo, useState } from 'react';

import { ehrNotesApi, RECORD_FAILED_CODE, type EhrNotesApi } from '../api/ehrNotes';
import { ALREADY_WRITTEN_CODE, NOT_LINKED_CODE } from '../api/ehrNotes';
import { notesApi, type Note, type NotesApi } from '../api/notes';
import { CodeList } from '../components/CodeList';
import { useNote } from '../hooks/useNote';
import {
  acceptSuggestion,
  addCode,
  diffNote,
  draftOf,
  hasEdits,
  removeCode,
  type NoteDraft,
} from '../notes/draft';
import { POLL_INTERVAL_MS } from '../notes/readiness';
import {
  WRITE_BACK_MESSAGES,
  writeBackState,
  writeBackView,
  type WriteAttempt,
} from '../notes/writeBack';

export interface NoteReviewScreenProps {
  /** Which visit's note. The only identifier the note routes are keyed on. */
  sessionId: string;
  /**
   * The SMART launch this page holds, or null.
   *
   * Null after a reload — a `launch_id` resolves to an EHR access token, so it
   * is never persisted. That is a real state, not a missing prop, and the EHR
   * write says so rather than failing when pressed.
   */
  launchId: string | null;
  /** The chart entry this visit corresponds to, or null when it has none. */
  ehrEncounterId: string | null;
  notes?: NotesApi;
  ehrNotes?: EhrNotesApi;
  /** Offered as a way back to the visit flow. */
  onStartAnotherVisit?: () => void;
}

const SECTIONS = [
  { key: 'soapSubjective', label: 'Subjective' },
  { key: 'soapObjective', label: 'Objective' },
  { key: 'soapAssessment', label: 'Assessment' },
  { key: 'soapPlan', label: 'Plan' },
] as const;

function Panel({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
      <h1 className="text-lg font-semibold text-slate-900">Note review</h1>
      {children}
    </main>
  );
}

function Action({
  label,
  onClick,
  testId,
  disabled = false,
}: {
  label: string;
  onClick: () => void;
  testId: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      data-testid={testId}
      className="self-start rounded-md bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700 disabled:bg-slate-300"
    >
      {label}
    </button>
  );
}

export function NoteReviewScreen({
  sessionId,
  launchId,
  ehrEncounterId,
  notes = notesApi,
  ehrNotes = ehrNotesApi,
  onStartAnotherVisit,
}: NoteReviewScreenProps) {
  const { state, retry, replace } = useNote(sessionId, notes);

  /**
   * The provider's unsaved edits, and the note they were made against.
   *
   * `draft` is null until the note loads and is reset to the server's answer on
   * every successful save, so the diff is always taken against what is stored
   * rather than against what was first displayed.
   */
  const [draft, setDraft] = useState<NoteDraft | null>(null);
  const [baseline, setBaseline] = useState<NoteDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState<WriteAttempt>({ kind: 'idle' });

  const note = state.kind === 'loaded' ? state.note : null;

  /**
   * Adopt a freshly loaded or freshly saved note as the baseline.
   *
   * Keyed on `note_id` and the server's own flags rather than on object
   * identity: a poll that returns the same note must not discard edits the
   * provider has typed in the meantime.
   */
  const signature =
    note === null ? null : `${note.noteId}:${String(note.providerEdited)}:${note.generatedAt}`;
  const [adopted, setAdopted] = useState<string | null>(null);
  if (note !== null && signature !== adopted) {
    setAdopted(signature);
    setBaseline(draftOf(note));
    setDraft(draftOf(note));
  }

  const dirty = baseline !== null && draft !== null && hasEdits(baseline, draft);

  const editSection = useCallback((key: (typeof SECTIONS)[number]['key'], value: string) => {
    setDraft((current) => (current === null ? current : { ...current, [key]: value }));
  }, []);

  const save = useCallback(async () => {
    if (baseline === null || draft === null) {
      return;
    }
    const patch = diffNote(baseline, draft);
    setSaving(true);
    setSaveError(null);
    const result = await notes.updateNote(sessionId, patch);
    setSaving(false);
    if (result.ok) {
      replace(result.value);
      return;
    }
    setSaveError(`The note could not be saved. ${result.failure.message}`);
  }, [baseline, draft, notes, replace, sessionId]);

  const markReviewed = useCallback(async () => {
    setSaving(true);
    setSaveError(null);
    // Only this field. The attestation is about what is stored, which is why the
    // button is unavailable while edits are unsaved — see its disabled state.
    const result = await notes.updateNote(sessionId, { reviewed_by_provider: true });
    setSaving(false);
    if (result.ok) {
      replace(result.value);
      return;
    }
    setSaveError(`The note could not be marked reviewed. ${result.failure.message}`);
  }, [notes, replace, sessionId]);

  const writeToEhr = useCallback(async () => {
    if (launchId === null) {
      return;
    }
    setAttempt({ kind: 'writing' });
    const result = await ehrNotes.writeNote(sessionId, launchId);
    if (result.ok) {
      // The note now carries the document the chart holds, which is what makes
      // `writeBackState` report it as filed on this render and after a reload.
      replace({ ...(note as Note), ehrDocumentRefId: result.value.ehrDocumentRefId });
      setAttempt({ kind: 'idle' });
      return;
    }

    const { failure } = result;
    if (failure.kind === 'status' && failure.code === RECORD_FAILED_CODE) {
      // The document exists on the chart and this system did not record it.
      // Terminal: the service's message names the document, and pressing again
      // would file a second copy of one visit's note.
      setAttempt({ kind: 'unrecorded', message: failure.message });
      return;
    }
    if (failure.kind === 'status' && failure.code === ALREADY_WRITTEN_CODE) {
      setAttempt({ kind: 'filed' });
      return;
    }
    if (failure.kind === 'status' && failure.code === NOT_LINKED_CODE) {
      setAttempt({ kind: 'not-linked' });
      return;
    }
    setAttempt({ kind: 'failed', message: failure.message });
  }, [ehrNotes, launchId, note, replace, sessionId]);

  const view = useMemo(
    () =>
      writeBackView(
        writeBackState({
          ehrDocumentRefId: note?.ehrDocumentRefId ?? null,
          launchId,
          ehrEncounterId,
        }),
        attempt,
      ),
    [attempt, ehrEncounterId, launchId, note],
  );

  if (state.kind === 'loading') {
    return (
      <Panel>
        <p className="text-sm text-slate-700" data-testid="note-loading">
          Loading the note…
        </p>
      </Panel>
    );
  }

  if (state.kind === 'pending') {
    return (
      <Panel>
        <p className="text-sm text-slate-700" data-testid="note-pending">
          The note for this visit is still being generated.
        </p>
        {state.exhausted ? (
          <>
            <p className="text-sm text-slate-600" data-testid="note-pending-exhausted">
              It has not arrived yet. It may still be generating, or the generation may have
              failed — MedAuth AI cannot tell those apart from here.
            </p>
            <Action label="Check again" onClick={retry} testId="note-retry" />
          </>
        ) : (
          <p className="text-sm text-slate-600">
            Checking every {Math.round(POLL_INTERVAL_MS / 1000)} seconds. This page updates on its
            own.
          </p>
        )}
      </Panel>
    );
  }

  if (state.kind === 'missing') {
    return (
      <Panel>
        {/*
          Deliberately not the same screen as `pending`. TASK-032 made these two
          different error codes so a review screen could tell a provider which
          happened, and collapsing them here would discard the distinction at the
          only place it was ever meant to be used.
        */}
        <p className="text-sm text-slate-700" data-testid="note-missing">
          This visit could not be found. It may have been ended on another device, or the link may
          be wrong. Nothing will change by waiting.
        </p>
        {onStartAnotherVisit !== undefined && (
          <Action label="Start a visit" onClick={onStartAnotherVisit} testId="start-another" />
        )}
      </Panel>
    );
  }

  if (state.kind === 'error') {
    return (
      <Panel>
        <p className="text-sm text-red-800" role="alert" data-testid="note-error">
          The note could not be loaded. {state.failure.message}
        </p>
        <Action label="Try again" onClick={retry} testId="note-retry" />
      </Panel>
    );
  }

  const loaded = state.note;
  const current = draft ?? draftOf(loaded);

  return (
    <Panel>
      <div className="flex flex-col gap-1">
        <p className="text-sm text-slate-600" data-testid="note-generated-at">
          Generated {loaded.generatedAt}
        </p>
        <p className="text-sm text-slate-600" data-testid="note-flags">
          {loaded.reviewedByProvider ? 'Reviewed by you' : 'Not yet reviewed'}
          {loaded.providerEdited ? ' · Edited' : ''}
        </p>
      </div>

      {SECTIONS.map(({ key, label }) => (
        <section key={key} className="flex flex-col gap-1">
          <label className="text-sm font-semibold text-slate-900" htmlFor={`section-${key}`}>
            {label}
          </label>
          <textarea
            id={`section-${key}`}
            data-testid={`section-${key}`}
            value={current[key] ?? ''}
            onChange={(event) => editSection(key, event.target.value)}
            rows={4}
            className="rounded-md border border-slate-300 p-2 text-sm"
          />
        </section>
      ))}

      <CodeList
        title="ICD-10 diagnoses"
        name="icd10"
        codes={current.icd10Codes}
        onAccept={(code) =>
          setDraft((d) => (d === null ? d : { ...d, icd10Codes: acceptSuggestion(d.icd10Codes, code) }))
        }
        onRemove={(code) =>
          setDraft((d) => (d === null ? d : { ...d, icd10Codes: removeCode(d.icd10Codes, code) }))
        }
        onAdd={(code) =>
          setDraft((d) => (d === null ? d : { ...d, icd10Codes: addCode(d.icd10Codes, code) }))
        }
      />

      <CodeList
        title="CPT procedures"
        name="cpt"
        codes={current.cptCodes}
        onAccept={(code) =>
          setDraft((d) => (d === null ? d : { ...d, cptCodes: acceptSuggestion(d.cptCodes, code) }))
        }
        onRemove={(code) =>
          setDraft((d) => (d === null ? d : { ...d, cptCodes: removeCode(d.cptCodes, code) }))
        }
        onAdd={(code) =>
          setDraft((d) => (d === null ? d : { ...d, cptCodes: addCode(d.cptCodes, code) }))
        }
      />

      {saveError !== null && (
        <p className="text-sm text-red-800" role="alert" data-testid="save-error">
          {saveError}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Action label="Save" onClick={save} testId="save-note" disabled={!dirty || saving} />
        <Action
          label="Mark reviewed"
          onClick={markReviewed}
          testId="mark-reviewed"
          disabled={loaded.reviewedByProvider || dirty || saving}
        />
      </div>
      {dirty && !loaded.reviewedByProvider && (
        <p className="text-sm text-slate-600" data-testid="review-blocked">
          Save your edits before marking the note reviewed — the attestation records what is
          stored, not what is on screen.
        </p>
      )}

      <section className="flex flex-col gap-2 border-t border-slate-200 pt-4">
        <h2 className="text-sm font-semibold text-slate-900">Patient’s chart</h2>
        {view.kind === 'available' && (
          <Action label="Write to EHR" onClick={writeToEhr} testId="write-to-ehr" />
        )}
        {view.kind === 'writing' && (
          <p className="text-sm text-slate-700" data-testid="write-in-progress">
            Filing the note to the chart…
          </p>
        )}
        {view.kind === 'filed' && (
          <p className="text-sm text-slate-700" data-testid="write-filed">
            {WRITE_BACK_MESSAGES.filed}
            {view.documentId !== null && ` Document ${view.documentId}.`}
          </p>
        )}
        {view.kind === 'no-launch' && (
          <p className="text-sm text-slate-700" data-testid="write-no-launch">
            {WRITE_BACK_MESSAGES['no-launch']}
          </p>
        )}
        {view.kind === 'not-linked' && (
          <p className="text-sm text-slate-700" data-testid="write-not-linked">
            {WRITE_BACK_MESSAGES['not-linked']}
          </p>
        )}
        {view.kind === 'unrecorded' && (
          // No button, permanently. The chart already changed and a second press
          // would file a duplicate; the service's message names the document.
          <p className="text-sm text-red-800" role="alert" data-testid="write-unrecorded">
            {view.message} Do not file this note again — it is already on the chart.
          </p>
        )}
        {view.kind === 'failed' && (
          <>
            <p className="text-sm text-red-800" role="alert" data-testid="write-failed">
              The note was not filed. {view.message}
            </p>
            <Action label="Try again" onClick={writeToEhr} testId="write-to-ehr" />
          </>
        )}
      </section>

      {onStartAnotherVisit !== undefined && (
        <Action label="Start another visit" onClick={onStartAnotherVisit} testId="start-another" />
      )}
    </Panel>
  );
}
