/**
 * The `/notes/:sessionId` route, and the one decision it makes (TASK-071).
 *
 * **Which note is read comes from the URL, never from memory.** That is what
 * makes the screen linkable and makes it survive a reload, which is the whole
 * reason this app took a router. The completed visit this page happens to be
 * holding is used only when it names the *same* session; otherwise this is a
 * link or a reload, and the screen renders with no launch and no chart entry —
 * both real states that it reports rather than failing on.
 *
 * Keeping that check here rather than inside the screen means the screen takes
 * three plain values and has nothing to reconcile.
 */

import { useParams } from 'react-router';

import type { CompletedVisit } from '../session/completedVisit';

import { NoteReviewScreen } from './NoteReviewScreen';

export interface NoteReviewRouteProps {
  /** The visit this page closed, if any. Consulted only when it matches the URL. */
  completed: CompletedVisit | null;
  /** The launch this page holds, which outlives any one encounter. */
  launchId: string | null;
  onStartAnotherVisit?: () => void;
}

export function NoteReviewRoute({
  completed,
  launchId,
  onStartAnotherVisit,
}: NoteReviewRouteProps) {
  const { sessionId } = useParams<{ sessionId: string }>();

  if (sessionId === undefined || sessionId === '') {
    // Not reachable through the route's own pattern, which requires the segment.
    // Reported rather than rendered as an empty note, because a request for
    // `/notes/` names no visit and guessing one would read the wrong chart.
    return (
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
        <h1 className="text-lg font-semibold text-slate-900">Note review</h1>
        <p className="text-sm text-slate-700" data-testid="note-no-session">
          This link does not name a visit.
        </p>
      </main>
    );
  }

  // The chart entry is context this page holds, not something the URL carries —
  // so it applies only to the visit it was recorded for. A link to another
  // session's note gets null, and the chart write says it holds no launch.
  const forThisSession = completed?.session.sessionId === sessionId ? completed : null;

  return (
    <NoteReviewScreen
      sessionId={sessionId}
      launchId={launchId}
      ehrEncounterId={forThisSession?.ehrEncounterId ?? null}
      {...(onStartAnotherVisit === undefined ? {} : { onStartAnotherVisit })}
    />
  );
}
