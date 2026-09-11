/**
 * The live transcript of the encounter (TASK-070).
 *
 * **The whole requirement of this component is that an empty pane never reads as
 * "nobody is speaking."** Redis pub/sub keeps no history, so a connection opened
 * late or reopened after a drop starts from silence and no earlier speech is
 * recoverable — TASK-041d states that as a property of the bus and deliberately
 * leaves replay out of scope. That makes "connected, nothing said yet" and "not
 * connected" produce the same empty list, and they are rendered differently
 * here, always, including the gap they leave behind after a reconnect.
 *
 * It is the same rule this repository applies to a payer's silence and to
 * `validation: null`: an absence of information is not information about an
 * absence.
 *
 * Nothing here logs. What it renders is what was said in a clinical encounter.
 */

import type { TranscriptStreamState } from '../hooks/useTranscriptStream';
import type { TranscriptSegment } from '../transcript/segment';

export interface TranscriptPaneProps {
  state: TranscriptStreamState;
  segments: TranscriptSegment[];
  /** Start a fresh connection attempt. Offered only while the stream is down. */
  onRetry: () => void;
}

/**
 * What the pane says about itself, above the words.
 *
 * Always rendered, never conditional on there being segments: the status line is
 * the thing that stops an empty pane from being ambiguous, so it cannot be the
 * thing that disappears when the pane is empty.
 */
function StatusLine({ state, count }: { state: TranscriptStreamState; count: number }) {
  if (state.status === 'connecting') {
    return (
      <p className="text-xs text-slate-500" data-testid="transcript-status">
        Connecting to the transcript…
      </p>
    );
  }

  if (state.status === 'error') {
    return (
      <p className="text-xs font-medium text-red-800" data-testid="transcript-status">
        {state.error.message}
      </p>
    );
  }

  return (
    <p className="text-xs text-slate-500" data-testid="transcript-status">
      {count === 0
        ? 'Connected. Nothing has been said yet.'
        : 'Connected. Showing speech from this connection onwards.'}
    </p>
  );
}

export function TranscriptPane({ state, segments, onRetry }: TranscriptPaneProps) {
  return (
    <section
      aria-label="Live transcript"
      className="flex min-h-48 flex-col gap-2 rounded-lg border border-slate-200 bg-white p-4"
    >
      <header className="flex items-baseline justify-between gap-4">
        <h2 className="text-sm font-semibold text-slate-900">Transcript</h2>
        {state.status === 'error' ? (
          <button
            type="button"
            onClick={onRetry}
            data-testid="retry-transcript"
            className="rounded border border-slate-300 px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
          >
            Reconnect
          </button>
        ) : null}
      </header>

      <StatusLine state={state} count={segments.length} />

      {/*
        A live region, so a provider using a screen reader is told what arrives
        rather than having to go looking for it. `polite` rather than `assertive`:
        speech arriving is not an alert, and interrupting for every utterance
        would make the nudges — which are assertive, and are the thing worth
        interrupting for — indistinguishable from ordinary conversation.
      */}
      <ol
        aria-live="polite"
        data-testid="transcript-segments"
        className="flex flex-col gap-1 overflow-y-auto text-sm leading-relaxed text-slate-800"
      >
        {segments.map((segment) => (
          <li key={segment.resultId}>{segment.text}</li>
        ))}
      </ol>
    </section>
  );
}
