/**
 * Procedures flagged during this visit, and what is still undocumented
 * (TASK-070).
 *
 * The nudge overlay is transient by design — a banner appears, the provider
 * reads it and dismisses it, and it is gone. This is the standing record of what
 * was raised, so a provider at the end of a consultation can see every procedure
 * the payer rules flagged rather than only the alerts still on screen.
 *
 * **Dismissing an alert does not tick anything off here, and nothing else in
 * this app does either.** Acknowledging a nudge records that a provider *saw*
 * it, which is a different fact from the documentation gap being filled —
 * `PATCH /nudges/{nudge_id}/acknowledge` says so in its own name. Nothing in
 * this repository observes a criterion becoming documented, so an item that
 * showed itself as resolved would be asserting something no service has
 * determined. Every entry therefore stays until the visit ends, and the heading
 * says what the list is rather than implying it is a task list that empties.
 *
 * **An empty `missingCriteria` is not "nothing missing".** On a fallback answer
 * — Qdrant unreachable, a Bedrock error, a retrieval that matched nothing — the
 * criteria were never retrieved, so the honest rendering is that they are
 * unknown and need confirming. This is the same distinction the banner draws,
 * and it has to be drawn again here because this list outlives the banner.
 */

import type { Nudge } from '@medauth/nudge-client';

export interface ProcedureChecklistProps {
  /** Every nudge raised this visit, in arrival order. Never only the live ones. */
  flagged: Nudge[];
}

export function ProcedureChecklist({ flagged }: ProcedureChecklistProps) {
  return (
    <section
      aria-label="Flagged procedures"
      className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-4"
    >
      <h2 className="text-sm font-semibold text-slate-900">Flagged this visit</h2>

      {flagged.length === 0 ? (
        // Deliberately worded as a fact about alerts rather than about coverage.
        // "Nothing needs prior authorization" would be a determination, and no
        // service has made one — a quiet stream and a clean encounter look the
        // same from here, which is why the transcript pane says so too.
        <p className="text-xs text-slate-500" data-testid="checklist-empty">
          No procedures have been flagged so far in this visit.
        </p>
      ) : (
        <ul className="flex flex-col gap-3" data-testid="checklist-items">
          {flagged.map((nudge) => (
            <li key={nudge.nudgeId} className="border-l-2 border-slate-300 pl-3">
              <p className="text-sm font-semibold text-slate-900">
                {nudge.procedure}
                {nudge.cptCode !== null && (
                  <span className="ml-2 font-mono text-xs font-normal text-slate-600">
                    CPT {nudge.cptCode}
                  </span>
                )}
              </p>

              {nudge.missingCriteria.length > 0 ? (
                <ul className="mt-1 list-disc pl-5 text-xs text-slate-700">
                  {nudge.missingCriteria.map((criterion) => (
                    <li key={criterion}>{criterion}</li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1 text-xs text-slate-700 italic">
                  No criteria list was available for this plan — confirm the requirements before
                  ordering.
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
