/**
 * A visit that has been closed, and the three things the note review screen
 * needs about it (TASK-071).
 *
 * **The whole point of this type is that they are three named fields.**
 * CLAUDE.md's "A SMART launch is not an encounter session" identifies three
 * identifiers with three different lifetimes, none derivable from another, and
 * this screen is the first place in `apps/web` that holds more than one at once:
 * `session_id` finds the note, `launch_id` holds the EHR credential the chart
 * write is made with, and `ehr_encounter_id` says whether there is a chart entry
 * to write to at all. Storing any two of them in one field named for either is
 * the collapse that document rejects.
 *
 * It is built in `SessionScreen`, which is the only place all three are known at
 * once: the session comes back from `POST /sessions/start`, and the launch and
 * the chart entry come off the `VisitSubject` the patient picker resolved.
 * Assembling it anywhere else would mean re-deriving one of them.
 *
 * **Never persisted.** A `launch_id` resolves to an EHR access token, so it is a
 * credential by this repository's definition and lives in memory for the life of
 * the page — which is why a reload leaves the review screen with no launch, and
 * why that is a state the screen renders rather than an error it reports.
 */

import type { Session } from '../api/sessions';

export interface CompletedVisit {
  /** The encounter session that was closed. Keys the note routes. */
  session: Session;
  /**
   * The SMART launch the visit ran under, or null for a visit started without
   * one. Goes in `X-MedAuth-Launch-Id` and nowhere else — never a URL.
   */
  launchId: string | null;
  /**
   * The encounter as the EHR knows it, or null when the visit has no chart entry.
   *
   * Null is an ordinary state: a standalone launch names no encounter. It is
   * what makes the chart write unavailable rather than merely failing when it is
   * pressed.
   */
  ehrEncounterId: string | null;
}
