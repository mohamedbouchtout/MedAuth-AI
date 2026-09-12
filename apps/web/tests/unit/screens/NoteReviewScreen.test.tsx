/**
 * The note review screen (TASK-071).
 *
 * Four properties here are requirements rather than presentation, and each has
 * its own test because each fails silently if it is wrong:
 *
 * - a save sends only what the provider changed, and never the code lists when
 *   they were untouched;
 * - loading the screen attests to nothing;
 * - a note that is not ready is a different screen from a visit that does not
 *   exist;
 * - the chart write is never a control whose only possible outcome is an error,
 *   and is never offered again once a document has been created.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { NoteReviewScreen } from '../../../src/screens/NoteReviewScreen';
import {
  aNote,
  ehrNotesThat,
  notesFailing,
  notesServing,
  SESSION_ID,
  suggestedCode,
  type FakeNotes,
} from '../../support/notes';

const LAUNCH = 'launch-7';
const ENCOUNTER = 'Encounter/7';

function renderScreen(
  notes: FakeNotes,
  overrides: Partial<React.ComponentProps<typeof NoteReviewScreen>> = {},
) {
  const ehrNotes = ehrNotesThat({
    ok: true,
    value: { sessionId: SESSION_ID, ehrDocumentRefId: 'DocumentReference/9' },
  });
  render(
    <NoteReviewScreen
      sessionId={SESSION_ID}
      launchId={LAUNCH}
      ehrEncounterId={ENCOUNTER}
      notes={notes}
      ehrNotes={ehrNotes}
      {...overrides}
    />,
  );
  return { notes, ehrNotes };
}

async function loaded(notes: FakeNotes, overrides = {}) {
  const rendered = renderScreen(notes, overrides);
  await waitFor(() => expect(screen.getByTestId('section-soapPlan')).toBeInTheDocument());
  return rendered;
}

describe('saving an edit', () => {
  it('sends only the section the provider changed', async () => {
    const notes = notesServing();
    await loaded(notes);

    fireEvent.change(screen.getByTestId('section-soapPlan'), {
      target: { value: 'MRI left knee.' },
    });
    fireEvent.click(screen.getByTestId('save-note'));

    await waitFor(() => expect(notes.updateNote).toHaveBeenCalled());
    expect(notes.updateNote).toHaveBeenCalledWith(SESSION_ID, { soap_plan: 'MRI left knee.' });
  });

  /**
   * The failure this whole contract exists to prevent, asserted at the screen
   * rather than only on the diff: a text edit on a note whose codes were never
   * extracted must send no code key at all. `[]` would declare the encounter has
   * no diagnoses and nothing anywhere would report it as an error.
   */
  it('sends no code key when the codes were not touched', async () => {
    const notes = notesServing(aNote({ icd10Codes: null, cptCodes: null }));
    await loaded(notes);

    fireEvent.change(screen.getByTestId('section-soapSubjective'), {
      target: { value: 'Left knee pain.' },
    });
    fireEvent.click(screen.getByTestId('save-note'));

    await waitFor(() => expect(notes.updateNote).toHaveBeenCalled());
    const patch = notes.updateNote.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(patch).not.toHaveProperty('icd10_codes');
    expect(patch).not.toHaveProperty('cpt_codes');
  });

  it('offers nothing to save until something is edited', async () => {
    await loaded(notesServing());

    expect(screen.getByTestId('save-note')).toBeDisabled();
  });

  it('reports a refused save', async () => {
    const notes = notesServing();
    notes.updateNote.mockResolvedValueOnce({
      ok: false,
      failure: { kind: 'status', status: 422, code: 'bad', message: 'rejected' },
    });
    await loaded(notes);

    fireEvent.change(screen.getByTestId('section-soapPlan'), { target: { value: 'x' } });
    fireEvent.click(screen.getByTestId('save-note'));

    await waitFor(() => expect(screen.getByTestId('save-error')).toHaveTextContent('rejected'));
  });
});

describe('attesting to a note', () => {
  /**
   * `reviewed_by_provider` records a human having read the note. Setting it on a
   * page load would make it record that a screen was opened — a distinction an
   * auditor cannot recover afterwards.
   */
  it('sends nothing merely by being opened', async () => {
    const notes = notesServing();
    await loaded(notes);

    expect(notes.updateNote).not.toHaveBeenCalled();
  });

  it('sends only the attestation when the provider marks it reviewed', async () => {
    const notes = notesServing();
    await loaded(notes);

    fireEvent.click(screen.getByTestId('mark-reviewed'));

    await waitFor(() => expect(notes.updateNote).toHaveBeenCalled());
    expect(notes.updateNote).toHaveBeenCalledWith(SESSION_ID, { reviewed_by_provider: true });
  });

  /**
   * The attestation is about what is stored, not about what is in a textarea. A
   * provider who could attest over unsaved edits would be signing something the
   * record does not contain.
   */
  it('will not attest while edits are unsaved, and says why', async () => {
    const notes = notesServing();
    await loaded(notes);

    fireEvent.change(screen.getByTestId('section-soapPlan'), { target: { value: 'x' } });

    expect(screen.getByTestId('mark-reviewed')).toBeDisabled();
    expect(screen.getByTestId('review-blocked')).toBeInTheDocument();
  });
});

describe('codes', () => {
  it('renders a Comprehend suggestion as a suggestion, not as a signed code', async () => {
    await loaded(notesServing());

    const entry = screen.getByTestId(`icd10-entry-${suggestedCode.code}`);
    expect(entry).toHaveAttribute('data-source', 'comprehend-medical');
    expect(screen.getByTestId(`icd10-source-${suggestedCode.code}`)).toHaveTextContent('Suggested');
    expect(screen.getByTestId(`icd10-accept-${suggestedCode.code}`)).toBeInTheDocument();
  });

  /**
   * Accepting is the mechanism by which a machine suggestion becomes claimable
   * by TASK-060 and sendable to a chart by TASK-053. The score goes: a human
   * acceptance is a fact, not a probability, and the server rejects an entry
   * that keeps one.
   */
  it('re-sends an accepted suggestion as provider-accepted with no confidence', async () => {
    const notes = notesServing();
    await loaded(notes);

    fireEvent.click(screen.getByTestId(`icd10-accept-${suggestedCode.code}`));
    fireEvent.click(screen.getByTestId('save-note'));

    await waitFor(() => expect(notes.updateNote).toHaveBeenCalled());
    const patch = notes.updateNote.mock.calls[0]?.[1] as { icd10_codes: unknown[] };
    expect(patch.icd10_codes[1]).toEqual({
      code: suggestedCode.code,
      display: suggestedCode.display,
      source: 'provider-accepted',
      confidence: null,
      validation: null,
    });
  });

  /**
   * `null` is "the extraction never answered" and `[]` is "it ran and found
   * nothing". Rendering the first as the second is the collapse the shape
   * contract exists to prevent, arriving through a UI instead of a request body.
   */
  it('says an unanswered list is unanswered rather than empty', async () => {
    await loaded(notesServing(aNote({ icd10Codes: null })));

    expect(screen.getByTestId('icd10-unanswered')).toBeInTheDocument();
    expect(screen.queryByTestId('icd10-empty')).not.toBeInTheDocument();
  });

  it('says an empty list ran and found nothing', async () => {
    await loaded(notesServing(aNote({ icd10Codes: [] })));

    expect(screen.getByTestId('icd10-empty')).toBeInTheDocument();
  });
});

describe('a note that is not ready', () => {
  /**
   * The ordinary state for a few seconds after a visit ends, while the Sonnet
   * call runs. Deliberately a different screen from an unknown visit.
   */
  it('says the note is being generated', async () => {
    renderScreen(notesFailing(404, 'note_not_generated'));

    await waitFor(() => expect(screen.getByTestId('note-pending')).toBeInTheDocument());
    expect(screen.queryByTestId('note-missing')).not.toBeInTheDocument();
  });

  it('says an unknown visit is unknown, and does not offer to wait', async () => {
    renderScreen(notesFailing(404, 'session_not_found'));

    await waitFor(() => expect(screen.getByTestId('note-missing')).toBeInTheDocument());
    expect(screen.queryByTestId('note-pending')).not.toBeInTheDocument();
  });

  it('reports a transport failure as one, with a retry', async () => {
    const notes: FakeNotes = {
      readNote: vi.fn(() =>
        Promise.resolve({ ok: false as const, failure: { kind: 'network' as const, message: 'down' } }),
      ),
      updateNote: vi.fn(),
    };
    renderScreen(notes);

    await waitFor(() => expect(screen.getByTestId('note-error')).toHaveTextContent('down'));
    expect(screen.getByTestId('note-retry')).toBeInTheDocument();
  });
});

describe('writing to the chart', () => {
  it('offers the write when a launch and a chart entry are both held', async () => {
    await loaded(notesServing());

    expect(screen.getByTestId('write-to-ehr')).toBeInTheDocument();
  });

  /**
   * `POST /fhir/notes` could only ever answer 422 here, so the button is not
   * offered — and the reason is stated rather than the control being hidden,
   * which would leave a provider unable to tell this from a broken build.
   */
  it('explains a visit with no chart entry instead of offering the write', async () => {
    await loaded(notesServing(), { ehrEncounterId: null });

    expect(screen.queryByTestId('write-to-ehr')).not.toBeInTheDocument();
    expect(screen.getByTestId('write-not-linked')).toBeInTheDocument();
  });

  /** A reload loses the launch, because a launch is a credential and is never persisted. */
  it('explains a page holding no launch, differently', async () => {
    await loaded(notesServing(), { launchId: null });

    expect(screen.queryByTestId('write-to-ehr')).not.toBeInTheDocument();
    expect(screen.getByTestId('write-no-launch')).toBeInTheDocument();
    expect(screen.queryByTestId('write-not-linked')).not.toBeInTheDocument();
  });

  it('files the note and then reports it as filed', async () => {
    const { ehrNotes } = await loaded(notesServing());

    fireEvent.click(screen.getByTestId('write-to-ehr'));

    await waitFor(() => expect(screen.getByTestId('write-filed')).toBeInTheDocument());
    expect(ehrNotes.writeNote).toHaveBeenCalledWith(SESSION_ID, LAUNCH);
    expect(screen.queryByTestId('write-to-ehr')).not.toBeInTheDocument();
  });

  /** A note already on the chart is shown as filed, not as an error. */
  it('shows a 409 as already filed', async () => {
    const ehrNotes = ehrNotesThat({
      ok: false,
      failure: {
        kind: 'status',
        status: 409,
        code: 'NOTE_ALREADY_WRITTEN_TO_EHR',
        message: 'already filed',
      },
    });
    await loaded(notesServing(), { ehrNotes });

    fireEvent.click(screen.getByTestId('write-to-ehr'));

    await waitFor(() => expect(screen.getByTestId('write-filed')).toBeInTheDocument());
    expect(screen.queryByTestId('write-failed')).not.toBeInTheDocument();
  });

  it('adopts a 422 as the standing reason', async () => {
    const ehrNotes = ehrNotesThat({
      ok: false,
      failure: {
        kind: 'status',
        status: 422,
        code: 'ENCOUNTER_NOT_LINKED_TO_EHR',
        message: 'no chart entry',
      },
    });
    await loaded(notesServing(), { ehrNotes });

    fireEvent.click(screen.getByTestId('write-to-ehr'));

    await waitFor(() => expect(screen.getByTestId('write-not-linked')).toBeInTheDocument());
    expect(screen.queryByTestId('write-to-ehr')).not.toBeInTheDocument();
  });

  /**
   * The document exists on the chart and this system did not record it. The
   * button must never come back: the next press files a second copy of one
   * visit's note, which is a clinician reading one version while another is
   * amended.
   */
  it('never offers the write again after a record failure', async () => {
    const message = 'Filed as DocumentReference/9 but the id could not be recorded.';
    const ehrNotes = ehrNotesThat({
      ok: false,
      failure: { kind: 'status', status: 502, code: 'EHR_NOTE_RECORD_FAILED', message },
    });
    await loaded(notesServing(), { ehrNotes });

    fireEvent.click(screen.getByTestId('write-to-ehr'));

    await waitFor(() => expect(screen.getByTestId('write-unrecorded')).toHaveTextContent(message));
    expect(screen.queryByTestId('write-to-ehr')).not.toBeInTheDocument();
  });

  it('offers a retry for a transient failure', async () => {
    const ehrNotes = ehrNotesThat({
      ok: false,
      failure: { kind: 'network', message: 'unreachable' },
    });
    await loaded(notesServing(), { ehrNotes });

    fireEvent.click(screen.getByTestId('write-to-ehr'));

    await waitFor(() => expect(screen.getByTestId('write-failed')).toHaveTextContent('unreachable'));
    expect(screen.getByTestId('write-to-ehr')).toBeInTheDocument();
  });

  /** Server-side truth, so it holds on a fresh load with no launch in hand. */
  it('reports an already-filed note on load, with its document id', async () => {
    await loaded(notesServing(aNote({ ehrDocumentRefId: 'DocumentReference/9' })), {
      launchId: null,
    });

    expect(screen.getByTestId('write-filed')).toHaveTextContent('DocumentReference/9');
  });
});
