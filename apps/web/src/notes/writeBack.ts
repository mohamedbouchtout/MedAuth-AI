/**
 * Whether this note can be filed to the chart, and if not, which reason
 * (TASK-071).
 *
 * **The requirement: never render a control whose only possible outcome is an
 * error.** `POST /fhir/notes` answers 422 `ENCOUNTER_NOT_LINKED_TO_EHR` for a
 * visit started outside a SMART launch, and there is no launch at all to send
 * for a page that has been reloaded. Both are knowable before the provider
 * presses anything, so both are said before they press anything.
 *
 * **And never solve that by hiding the control.** A provider who expected to
 * file a note and sees nothing cannot tell "this visit has no chart entry" from
 * "this build is broken" from "I am on the wrong screen". Two genuinely
 * different situations get two legible states — the same call TASK-032 made
 * server-side in keeping `note_not_generated` distinct from `session_not_found`,
 * and the same rule this repository applies to a payer's silence and to
 * `validation: null`.
 *
 * The states are ordered, and the order is load-bearing: see `writeBackState`.
 */

/** What the screen offers for the EHR write, and why. */
export type WriteBackState =
  /** A launch is held and the visit has a chart entry. The button is live. */
  | { kind: 'available' }
  /**
   * Already on the chart.
   *
   * Server-side truth, from the note's own `ehr_document_ref_id`, so it survives
   * a reload in a way the other two states do not. A repeat write is refused by
   * the service and would be duplicate clinical documentation if it were not.
   */
  | { kind: 'filed'; documentId: string }
  /** The page holds no SMART launch — typically because the tab was reloaded. */
  | { kind: 'no-launch' }
  /** The visit was started outside a SMART launch, so there is no chart entry. */
  | { kind: 'not-linked' };

export interface WriteBackInputs {
  /** From the note itself. Non-null means the chart already holds this note. */
  ehrDocumentRefId: string | null;
  /** The SMART launch this page holds, or null. Never persisted — it is a credential. */
  launchId: string | null;
  /** The chart entry this visit corresponds to, or null when it has none. */
  ehrEncounterId: string | null;
}

/**
 * Decide which of the four states this note is in.
 *
 * **The order matters and is not arbitrary.**
 *
 * `filed` comes first because it is the only one the server knows: it is read
 * off the note rather than out of this page's memory, so it is still correct
 * after a reload when both of the others have lost their inputs.
 *
 * `no-launch` comes before `not-linked` because after a reload this page holds
 * neither the launch nor the chart entry, and reporting the missing launch is
 * the one a provider can act on — launching again from the chart restores both,
 * and then the screen can say truthfully whether the visit has a chart entry.
 * Reporting "no chart entry" there would be a guess dressed as a fact.
 */
export function writeBackState({
  ehrDocumentRefId,
  launchId,
  ehrEncounterId,
}: WriteBackInputs): WriteBackState {
  if (ehrDocumentRefId !== null) {
    return { kind: 'filed', documentId: ehrDocumentRefId };
  }
  if (launchId === null) {
    return { kind: 'no-launch' };
  }
  if (ehrEncounterId === null) {
    return { kind: 'not-linked' };
  }
  return { kind: 'available' };
}

/**
 * What the provider is told in each unavailable state.
 *
 * Each says what is true and what would change it. None of them says "an error
 * occurred", because none of these is an error — they are three ordinary facts
 * about a visit and a browser tab.
 */
export const WRITE_BACK_MESSAGES = {
  filed:
    'This note is already on the patient’s chart. MedAuth AI will not file it a second time — two copies of one visit’s note is a chart a clinician has to reconcile by hand.',
  'no-launch':
    'This page no longer holds the EHR sign-in, so the note cannot be filed from here. Launch MedAuth AI again from the patient’s chart to file it.',
  'not-linked':
    'This visit was not started from a patient’s chart, so there is no chart entry to file the note against. Start the visit from the EHR to file notes to it.',
} as const;

/**
 * What a press of the button produced, layered over the state above.
 *
 * Separate from `WriteBackState` because these are facts about this page's last
 * attempt rather than about the note, and two of them are terminal in a way no
 * reload preserves.
 */
export type WriteAttempt =
  /** Nothing has been pressed. */
  | { kind: 'idle' }
  | { kind: 'writing' }
  /**
   * The service answered 409: the note is already on the chart.
   *
   * The document id is not carried, because the 409 does not return one and
   * inventing a value to fill the field is the fabrication this repository
   * refuses everywhere else. A reload reads the real id off the note.
   */
  | { kind: 'filed' }
  /** 422 — the service says this encounter has no chart entry after all. */
  | { kind: 'not-linked' }
  /**
   * 502 `EHR_NOTE_RECORD_FAILED`. The document **was** created.
   *
   * Terminal, and the one outcome that must never leave the button pressable: a
   * second attempt files a second copy of one encounter's note on a patient's
   * chart. The message names the created document and comes from the service.
   */
  | { kind: 'unrecorded'; message: string }
  /** Anything else. Transient, and retrying is reasonable. */
  | { kind: 'failed'; message: string };

/** What the screen actually renders for the EHR write. */
export type WriteBackView =
  | { kind: 'available' }
  | { kind: 'writing' }
  | { kind: 'filed'; documentId: string | null }
  | { kind: 'unrecorded'; message: string }
  | { kind: 'no-launch' }
  | { kind: 'not-linked' }
  | { kind: 'failed'; message: string };

/**
 * Combine what is known about the note with what this page last attempted.
 *
 * **`unrecorded` outranks everything**, including the note's own state, because
 * it is the only outcome where the chart changed and this system does not know
 * it. Letting any later state supersede it would put the button back in front of
 * a provider whose next press files a duplicate.
 *
 * **The note's own `filed` outranks the rest of the attempt states** because it
 * is the server's answer rather than this page's, and it carries the document
 * id that a 409 does not.
 *
 * A transient failure is reported only where the action was genuinely available;
 * anywhere else the standing reason the write cannot happen is the more useful
 * thing to say.
 */
export function writeBackView(base: WriteBackState, attempt: WriteAttempt): WriteBackView {
  if (attempt.kind === 'unrecorded') {
    return { kind: 'unrecorded', message: attempt.message };
  }
  if (base.kind === 'filed') {
    return { kind: 'filed', documentId: base.documentId };
  }
  switch (attempt.kind) {
    case 'filed':
      return { kind: 'filed', documentId: null };
    case 'not-linked':
      return { kind: 'not-linked' };
    case 'writing':
      return { kind: 'writing' };
    case 'failed':
      return base.kind === 'available' ? { kind: 'failed', message: attempt.message } : base;
    case 'idle':
      return base;
    default: {
      const unhandled: never = attempt;
      void unhandled;
      return base;
    }
  }
}
